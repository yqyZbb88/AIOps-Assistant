"""聊天链路 ChatService：Agent-first + 关键词兜底。

承接原 ops_bot/bot.py 的 get_reply 与关键词规则：
    主链路  用户消息 → Agent（DeepSeek + 监控工具，见 src/agent/assistant.py）；
            模型自主判断是否查 CPU/内存/磁盘/告警。
    兜底    Agent 构建/调用不可用（未装 langchain、.env 缺 key、网络失败）→
            本模块关键词规则：CPU/内存/磁盘/告警/状态/帮助，仍可答复监控指标。
    未命中 → DEFAULT_REPLY。

重构要点：
- 关键词 handler 转发 src/core/capabilities.py 的纯函数（与 Agent 工具同一套能力实现），
  绑定「注入的 MonitorSource」，不再 import 具体监控模块（可注入 FakeMonitor）；
- Agent 经 `assistant_factory` 注入（container 提供），ChatService 不感知 langchain：
  首次聊天时尝试构建，失败即回落并标记，服务在无 key / 无 langchain 环境照常运行。

扩展方式：
    - 新增监控能力：先在 src/core/capabilities.py 加一个 `xxx_text(monitor)` 纯函数，
      再到本模块 RULES 与 agent/tools.py build_tools 各转发一行（两处产出同款文案）；
    - 关键词兜底新增"状态/帮助"类静态规则：向 __init__ 里 RULES 追加 Rule(keyword, reply=...)。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from src.core import capabilities
from src.interfaces.approval import (
    ApprovalError,
    ApprovalOutcome,
    ApprovalUnavailableError,
    PendingApproval,
)
from src.interfaces.assistant import ApprovalCancellable, ApprovalCapable, Assistant
from src.interfaces.monitor import MonitorSource
from src.interfaces.session import (
    DEFAULT_MESSAGE_WINDOW,
    STATUS_ABANDONED,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
    ChatMessage,
    ChatReply,
    HistoryCapable,
    SessionError,
    SessionNotFoundError,
    SessionStore,
    TurnResult,
)

logger = logging.getLogger("ops_agent.chat")

# 帮助信息：列出现阶段支持的能力
HELP_TEXT = (
    "可用能力：\n"
    "  - AI 对话（默认）：直接问任意运维问题，我会按需查询监控数据回答\n"
    "  - 监控指标：CPU / 内存 / 磁盘（AI 会自动查询 Node Exporter 数据）\n"
    "  - 告警：查询当前 pending / firing 状态的告警\n"
    "  - 帮助：显示本帮助信息\n"
    "\n（提示：LLM 不可用时会回落到关键词模式，仅支持固定命令）\n"
    "\n示例：\n"
    '  {"user": "ops", "message": "帮我看看现在系统负载高不高？"}'
)

# 兜底回复：LLM 不可用时的最终兜底
DEFAULT_REPLY = "我不理解，请尝试其他问题"


class Rule:
    """一条关键词规则：消息命中 keyword 时，返回回复。

    reply：静态字符串；handler：无参可调用对象（运行时动态计算回复，如查询指标）。
    """

    def __init__(
        self,
        keyword: str | tuple[str, ...],
        reply: str | None = None,
        handler: Callable[[], str] | None = None,
        description: str = "",
    ) -> None:
        self.keywords: tuple[str, ...] = (
            (keyword,) if isinstance(keyword, str) else tuple(keyword)
        )
        self.reply = reply
        self.handler = handler
        self.description = description or f"命中关键词：{'/'.join(self.keywords)}"

    def match(self, message: str) -> bool:
        """子串匹配：任一触发词出现在消息中即命中。"""
        return any(keyword in message for keyword in self.keywords)

    def respond(self) -> str:
        """计算该规则回复：优先动态 handler，否则返回静态 reply。"""
        if self.handler is not None:
            return self.handler()
        return self.reply or ""


class ChatService:
    """聊天主服务：Agent-first → 关键词兜底 → 默认回复。"""

    def __init__(
        self,
        monitor: MonitorSource,
        *,
        assistant_factory: Callable[[], Assistant] | None = None,
        sessions: SessionStore | None = None,
        message_window: int = DEFAULT_MESSAGE_WINDOW,
    ) -> None:
        self.monitor = monitor
        # 注入的 Agent 工厂：返回 Assistant 契约对象；失败抛异常即回落
        self._assistant_factory = assistant_factory
        self._assistant: Assistant | None = None
        self._agent_broken = assistant_factory is None
        # 只护「首次构建」，见 _ensure_assistant 的 docstring
        self._assistant_lock = threading.Lock()
        # 会话存储（可选）：None = 不落库，聊天与兜底全部照常
        self._sessions = sessions
        self._message_window = max(int(message_window), 1)
        # 规则按优先级从高到低排列，第一条命中生效；动态 handler 与 Agent 工具转发
        # 同一批 capabilities 纯函数（src/core/capabilities.py），无 LLM 时产出同款规范文案
        self._rules = [
            Rule(
                "CPU",
                handler=lambda: capabilities.cpu_usage_text(self.monitor),
                description="查询 CPU 使用率",
            ),
            Rule(
                "内存",
                handler=lambda: capabilities.memory_usage_text(self.monitor),
                description="查询内存使用率",
            ),
            Rule(
                "磁盘",
                handler=lambda: capabilities.disk_usage_text(self.monitor),
                description="查询磁盘使用率",
            ),
            # 告警状态：主动查询监控源（/api/v1/alerts），支持中文/英文触发词
            Rule(
                ("告警", "pending", "firing"),
                handler=lambda: capabilities.active_alerts_text(self.monitor),
                description="查询当前 pending/firing 告警",
            ),
            Rule("状态", reply="系统运行正常（模拟）", description="查询系统运行状态"),
            Rule("帮助", reply=HELP_TEXT, description="显示帮助信息"),
        ]

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def reply(self, message: str) -> str:
        """单轮回复（原契约不变：不带会话、不落库）。"""
        return self.reply_turn(message).reply

    def reply_turn(
        self,
        message: str,
        *,
        session_id: str | None = None,
        session_name: str | None = None,
        user: str = "",
    ) -> ChatReply:
        """带会话的一轮：Agent-first → 关键词兜底，并把这一轮写进会话表。

        三个刻意的结构选择：

        1. **落库放在 Agent 的 try/except 之外**。上面那条 catch-all 会吞掉一切异常并
           回落关键词，若把 DB 写也圈进去，任何持久化 bug 都会被归类成「LLM 抖动」而永远
           查不出来。这里 Agent 调用单独包 try，落库在 try 外、有自己的错误处理。
        2. **关键词兜底的回答同样落库**。落库若只发生在 Agent 分支里，那么「没装 langchain /
           没 key」的降级部署里会话表永远是空的，同一功能在两套部署下行为不同——而这个
           仓库把降级部署当一等公民（有专门的 no-langchain 回归测试）。
        3. **持久化不可用时只是不落库**，聊天照常（`_sessions is None` → 全程跳过）。
        """
        sid, sname = self._resolve_session(session_id, session_name, user)
        history = self._load_history(sid)

        turn: TurnResult | None = None
        assistant = self._ensure_assistant()
        if assistant is not None:
            try:
                turn = self._agent_turn(assistant, message, history)
            except Exception as exc:  # Agent 调用失败（网络等）→ 本轮回落关键词
                logger.warning("Agent 回复异常，回落关键词引擎: %s", exc)
        if turn is None:
            turn = self._fallback_turn(message)

        self._record_turn(sid, message, turn)
        return ChatReply(
            reply=turn.text,
            session_id=sid,
            session_name=sname or None,
            approval_id=turn.approval_id,
        )

    # ------------------------------------------------------------------
    # 会话（可选能力：存储不可用时全程跳过，行为与改造前一致）
    # ------------------------------------------------------------------
    @staticmethod
    def _agent_turn(
        assistant: Assistant, message: str, history: list[ChatMessage]
    ) -> TurnResult:
        """让 Agent 跑一轮。

        支持历史的实现走 reply_turn；只实现了 reply 的旧对象退回单轮
        （**必须靠 isinstance 分派，不能给 reply 加 history kwarg**：runtime_checkable
        只查方法名不查签名，加 kwarg 时旧对象会被误判为支持，然后在调用处 TypeError）。
        """
        if isinstance(assistant, HistoryCapable):
            return assistant.reply_turn(message, history=history)
        return TurnResult(text=assistant.reply(message))

    def _fallback_turn(self, message: str) -> TurnResult:
        for rule in self._rules:
            if rule.match(message):
                return TurnResult(text=rule.respond())
        return TurnResult(text=DEFAULT_REPLY)

    def _resolve_session(
        self, session_id: str | None, session_name: str | None, user: str
    ) -> tuple[str | None, str]:
        """确定本轮用哪个会话，返回 (session_id, session_name)；存储不可用 → (None, "")。

        三条路径的语义**刻意不同**：

        - **给了 `session_id`（服务端生成的随机 id）**：必须已存在，否则
          `SessionNotFoundError`（api 转 404）。不静默重建，否则 `DELETE` 形同虚设。
        - **给了 `session_name`（客户端自定义别名）**：没有就**新建**，这是它的全部意义
          ——客户端想用一个自己起的名字，不该被迫先拿一个随机 id。
        - **都没给**：新建一个未命名会话（与改造前行为一致）。

        命名会话按 **(user, name)** 查，所以 A 的 `test_1` 与 B 的 `test_1` 互不干扰。
        """
        store = self._sessions
        if store is None:
            return None, ""
        try:
            if session_id:
                info = store.get(session_id)
                if info is None:
                    raise SessionNotFoundError(f"会话不存在或已过期: {session_id}")
                return info.session_id, info.name
            if session_name:
                existing = store.find_by_name(user, session_name)
                if existing is not None:
                    return existing.session_id, existing.name
                # 并发下这里可能撞上唯一索引——store.create 会回查并复用，不会抛
                created = store.create(user=user, name=session_name)
                return created.session_id, created.name
            created = store.create(user=user)
            return created.session_id, created.name
        except SessionNotFoundError:
            raise
        except SessionError as exc:
            logger.error("会话存储不可用，本轮不落库: %s", exc)
            return None, ""

    def _load_history(self, session_id: str | None) -> list[ChatMessage]:
        """取可进模型窗口的历史；读失败降级为空历史（少记性，但不影响本轮回答）。"""
        if self._sessions is None or session_id is None:
            return []
        try:
            return self._sessions.history(session_id, limit=self._message_window)
        except SessionError as exc:
            logger.warning("读取会话历史失败，本轮按无历史处理: %s", exc, exc_info=True)
            return []

    def _record_turn(self, session_id: str | None, message: str, turn: TurnResult) -> None:
        """把一轮写进会话表（user + assistant 同事务）。

        失败**只记日志不抛**：聊天已经答完了，不能因为落库失败就让用户拿不到回答。
        代价是「历史表是唯一真相」在失败时会缺一块——所以必须记 ERROR 并让
        `/health` 能反映出 degraded，不能静默。
        """
        if self._sessions is None or session_id is None:
            return
        pending = turn.status == "pending_approval"
        try:
            self._sessions.append_turn(
                session_id,
                user_content=message,
                assistant_content=turn.text,
                status=STATUS_PENDING_APPROVAL if pending else STATUS_FINAL,
                approval_id=turn.approval_id,
            )
        except SessionError as exc:
            logger.error("写入会话历史失败（本轮未落库，历史将缺一块）: %s", exc)

    @property
    def storage_healthy(self) -> bool:
        """持久化是否正常（供 /health 暴露降级）。未启用存储时视为正常。"""
        return self._sessions is None or self._sessions.healthy

    # ------------------------------------------------------------------
    # 人工审批转发（写命令能力为可选；不支持时给出明确语义）
    # ------------------------------------------------------------------
    def pending_approvals(self) -> list[PendingApproval]:
        """当前待人工决策的写命令调用；Agent 未构建/不支持审批时返回空列表。

        **刻意走已建好的 self._assistant**，不调用 _ensure_assistant()：
        审批查询是旁路操作，不该为它触发 Agent 构建，也不该在构建失败时
        把 _agent_broken 置位（否则一次空查询就会拖垮后续聊天）。
        """
        assistant = self._assistant
        if assistant is None or not isinstance(assistant, ApprovalCapable):
            return []
        try:
            return assistant.pending_approvals()
        except Exception as exc:
            logger.warning("查询待审批项失败: %s", exc)
            return []

    def decide(self, approval_id: str, *, action: str, reason: str = "") -> ApprovalOutcome:
        """提交一次审批决策；Agent 不支持审批时抛 ApprovalError（由 api 转 503/404）。

        决策跑完后要**回填会话表**：中断那轮的最终回答是在这里才产生的，不回填的话
        历史里就会永远留一条「待审批」没有下文。三个出口都要处理——
        跑完（final）/ 又触发下一条（链式 pending）/ 抛错（保持 pending 不动）。
        """
        assistant = self._assistant
        if assistant is None or not isinstance(assistant, ApprovalCapable):
            raise ApprovalUnavailableError(
                "Agent 未启用写命令能力（config/agent_actions.yaml 未启用，或聊天尚未构建 Agent）"
            )
        try:
            outcome = assistant.decide(approval_id, action=action, reason=reason)
        except ApprovalUnavailableError:
            raise  # 能力没开 ≠ 这一条失效，不能把它标成 abandoned
        except ApprovalError:
            # 未知 / 已过期 / 已被处理：审批队列里已经没有了，历史里那条不可能再有下文
            self._abandon_pending(approval_id)
            raise
        self._resolve_pending(approval_id, outcome)
        return outcome

    def cancel_pending(self, approval_id: str) -> bool:
        """作废一条待审批项（会话被删除时用）。Agent 不支持时返回 False。

        与 pending_approvals/decide 同款：走**已建好的** `self._assistant`，
        不触发 Agent 构建、不置 `_agent_broken`。
        """
        assistant = self._assistant
        if assistant is None or not isinstance(assistant, ApprovalCancellable):
            return False
        try:
            return assistant.cancel_pending(approval_id)
        except Exception as exc:
            logger.warning("作废待审批项失败 id=%s: %s", approval_id, exc)
            return False

    def _resolve_pending(self, approval_id: str, outcome: ApprovalOutcome) -> None:
        """把审批结果回填到会话表。"""
        if self._sessions is None:
            return
        chained = outcome.status == "pending_approval"
        try:
            self._sessions.update_pending(
                approval_id,
                content=outcome.reply,
                status=STATUS_PENDING_APPROVAL if chained else STATUS_FINAL,
                next_approval_id=outcome.next_approval_id,
            )
        except SessionError as exc:
            logger.error("回填审批结果失败（历史里该轮将停在待审批态）: %s", exc)

    def _abandon_pending(self, approval_id: str) -> None:
        """把一条不可能再有下文的待审批轮标为 abandoned。"""
        if self._sessions is None:
            return
        try:
            self._sessions.update_pending(
                approval_id, content="", status=STATUS_ABANDONED
            )
        except SessionError as exc:
            logger.warning("标记待审批轮作废失败 id=%s: %s", approval_id, exc)

    def _ensure_assistant(self) -> Assistant | None:
        """首次聊天时经工厂构建 Agent 并缓存；构建失败标记 broken 后回落。

        **必须持锁 + 双检**：ChatService 是单例，而 FastAPI 的同步路由跑在线程池里，
        两个并发请求会同时看到 `_assistant is None` 各建一个 Agent——那不只是多建一个
        对象：工厂里还会各建一份 `ApprovalStore` 与 `InMemorySaver`，于是**待审批项与
        checkpoint 线程会分散到两个实例里**，表现为「刚批准的命令找不到线程」。
        锁只护首次构建，构建完成后走无锁快路径。
        """
        if self._assistant is not None:
            return self._assistant
        if self._agent_broken or self._assistant_factory is None:
            return None
        with self._assistant_lock:
            # 双检：等锁期间可能已被别的线程建好了
            if self._assistant is not None:
                return self._assistant
            if self._agent_broken:
                return None
            try:
                self._assistant = self._assistant_factory()
            except Exception as exc:  # 未装 langchain / .env 缺 key / init 失败等
                self._agent_broken = True
                logger.warning("Agent 构建不可用，回落关键词引擎: %s", exc)
            return self._assistant
