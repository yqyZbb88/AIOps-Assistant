"""聊天服务：优先使用 Agent，失败时回退关键词规则。

主链路：用户消息 -> Agent -> 按需查询监控或诊断工具 -> 回答。
回退链路：Agent 不可用时，使用关键词规则回答 CPU、内存、磁盘、告警等常见问题。
监控查询和格式化逻辑与 Agent 工具共用 capabilities。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from src.core import capabilities
from src.interfaces.approval import (
    ApprovalError,
    ApprovalOutcome,
    ApprovalUnavailableError,
    ApprovalOutcomeUnknownError,
    PendingApproval,
)
from src.interfaces.assistant import (
    ApprovalCancellable,
    ApprovalCapable,
    ApprovalContextCapable,
    ApprovalDecisionContextCapable,
    Assistant,
)
from src.interfaces.metric import MetricDefinition, MetricsConfig
from src.interfaces.monitor import MonitorSource
from src.interfaces.host import HostRegistry
from src.plugins.monitors.prometheus_catalog import METRIC_CATALOG
from src.utils.metrics_config import default_metrics_config
from src.utils.security import redact_text
from src.interfaces.session import (
    DEFAULT_MESSAGE_WINDOW,
    STATUS_ABANDONED,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
    STATUS_UNKNOWN,
    ChatMessage,
    ChatReply,
    HistoryCapable,
    SessionError,
    SessionNotFoundError,
    SessionStore,
    TurnResult,
    is_owner,
)

logger = logging.getLogger("aiops_assistant.chat")
_AGENT_RETRY_SECONDS = 60

# 帮助信息：列出当前可用能力
HELP_TEXT = (
    "可用能力：\n"
    "  - AI 对话（默认）：直接问任意运维问题，我会按需查询监控数据回答\n"
    "  - 监控指标：CPU / 内存 / 磁盘（AI 会自动查询 Node Exporter 数据）\n"
    "  - 告警：查询当前 pending / firing 状态的告警\n"
    "  - 帮助：显示本帮助信息\n"
    "\n（提示：LLM 不可用时会回落到关键词模式，仅支持固定命令）\n"
    "\n用法（两段式：先建会话拿到 id，之后每轮都带上它）：\n"
    '  1) POST /sessions  {"name": "生产排查"}          →  {"session_id": "..."}\n'
    '  2) POST /chat      {"message": "...", "session_id": "<上一步的 id>"}\n'
    "\n服务端根据 Authorization 头中的密钥识别身份；"
    "请求体不能指定身份。"
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
        metrics_config: MetricsConfig | None = None,
        catalog: tuple[MetricDefinition, ...] = METRIC_CATALOG,
        hosts: HostRegistry | None = None,
        host_assistant_factory: Callable[[str], Assistant] | None = None,
    ) -> None:
        self.monitor = monitor
        # 注入的 Agent 工厂：返回 Assistant 契约对象；失败抛异常即回落
        self._assistant_factory = assistant_factory
        self._host_assistant_factory = host_assistant_factory
        self._assistant: Assistant | None = None
        self._host_assistants: dict[str, Assistant] = {}
        self._host_agent_broken: set[str] = set()
        self._host_agent_retry_at: dict[str, float] = {}
        self._agent_retry_at = 0.0
        self._agent_broken = assistant_factory is None and host_assistant_factory is None
        self._hosts = hosts
        # 只护「首次构建」，见 _ensure_assistant 的 docstring
        self._assistant_lock = threading.Lock()
        # 会话存储（可选）：None = 不落库，聊天与兜底全部照常
        self._sessions = sessions
        self._message_window = max(int(message_window), 1)
        self._metrics_config = metrics_config or default_metrics_config()
        definitions = {definition.metric_id: definition for definition in catalog}
        self._metric_definitions = [
            definitions[runtime.metric_id]
            for runtime in self._metrics_config.selected("chat")
            if runtime.metric_id in definitions
        ]
        self._rules = []
        for definition in self._metric_definitions:
            if self._hosts is not None:
                continue

            def _handler(_definition=definition) -> str:
                return capabilities.metric_text(self.monitor, _definition)

            keywords = tuple(dict.fromkeys((definition.display_name, *definition.aliases)))
            self._rules.append(
                Rule(
                    keywords,
                    handler=_handler,
                    description=f"查询{definition.display_name}",
                )
            )
        self._rules.extend(
            [
                Rule("状态", reply="系统运行正常（模拟）", description="查询系统运行状态"),
                Rule("帮助", reply=HELP_TEXT, description="显示帮助信息"),
            ]
        )

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
        user: str | None = None,
    ) -> ChatReply:
        """按会话调用 Agent 或关键词兜底，并单独处理历史写入。

        user 只校验归属，选中会话只认 session_id。模型与存储异常分开处理，避免把
        写库失败误当模型失败。无存储兼容仅供内部调用；HTTP 预先要求可验证会话。
        """
        message = redact_text(message)
        sid, sname, host_id = self._resolve_session(session_id, user)
        history = self._load_history(sid)

        turn: TurnResult | None = None
        assistant = self._ensure_assistant(host_id)
        if assistant is not None:
            try:
                turn = self._agent_turn(
                    assistant, message, history, session_id=sid, user=user
                )
            except Exception as exc:  # Agent 调用失败（网络等）→ 本轮回落关键词
                logger.warning("Agent 回复异常，回落关键词引擎: %s", exc)
        if turn is None:
            turn = self._fallback_turn(message, host_id=host_id)

        self._record_turn(sid, message, turn)
        return ChatReply(
            reply=redact_text(turn.text),
            session_id=sid,
            session_name=sname or None,
            approval_id=turn.approval_id,
        )

    # ------------------------------------------------------------------
    # 会话（可选能力：存储不可用时全程跳过，行为与改造前一致）
    # ------------------------------------------------------------------
    @staticmethod
    def _agent_turn(
        assistant: Assistant,
        message: str,
        history: list[ChatMessage],
        *,
        session_id: str | None = None,
        user: str | None = None,
    ) -> TurnResult:
        """按可选协议传入审批上下文、历史，或退回单轮 reply。

        runtime_checkable 不检查方法签名，新能力使用独立方法名，避免旧实现被误判。
        """
        if isinstance(assistant, ApprovalContextCapable):
            return assistant.reply_turn_ctx(
                message, history=history, session_id=session_id, user=user
            )
        if isinstance(assistant, HistoryCapable):
            return assistant.reply_turn(message, history=history)
        return TurnResult(text=assistant.reply(message))

    def _fallback_turn(self, message: str, *, host_id: str = "") -> TurnResult:
        if self._hosts is not None and host_id:
            host = self._hosts.require(host_id)
            for definition in self._metric_definitions:
                keywords = (definition.display_name, *definition.aliases)
                if any(keyword in message for keyword in keywords):
                    return TurnResult(
                        text=capabilities.metric_text(
                            self.monitor, definition, target=host.monitor
                        )
                    )
        for rule in self._rules:
            if rule.match(message):
                return TurnResult(text=rule.respond())
        return TurnResult(text=DEFAULT_REPLY)

    def _resolve_session(
        self, session_id: str | None, user: str | None
    ) -> tuple[str | None, str, str]:
        """返回 (session_id, name, host_id)，内部无存储调用返回 (None, "", "")。

        已给 ID 必须存在且属于调用者，否则 SessionNotFoundError，不静默重建。未给 ID
        的内部兼容路径可创建会话；HTTP /chat 要求既有 ID，身份及主机访问由边界验证。
        """
        store = self._sessions
        if store is None:
            default_host = (
                self._hosts.enabled()[0].host_id
                if self._hosts is not None and len(self._hosts.enabled()) == 1
                else ""
            )
            return None, "", default_host
        try:
            if session_id:
                info = store.get(session_id)
                if info is None or not is_owner(info, user):
                    raise SessionNotFoundError(f"会话不存在或已过期: {session_id}")
                return info.session_id, info.name, info.host_id
            enabled = self._hosts.enabled() if self._hosts is not None else ()
            host_id = enabled[0].host_id if len(enabled) == 1 else ""
            created = store.create(user=user, host_id=host_id)
            return created.session_id, created.name, created.host_id
        except SessionNotFoundError:
            raise
        except SessionError as exc:
            if session_id and user is not None:
                raise
            logger.error("会话存储不可用，本轮不落库: %s", exc)
            return None, "", ""

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

        普通回复写失败只记日志并保留回答；会话已删除时返回不存在。
        待审批轮次必须保存：失败时作废申请并上抛，不能留下无会话记录的可执行申请。
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
            if pending:
                if turn.approval_id and not self.cancel_pending(turn.approval_id):
                    logger.error("未能作废未落库的审批 id=%s，需核对审批队列", turn.approval_id)
                raise
            if isinstance(exc, SessionNotFoundError):
                raise

    @property
    def storage_healthy(self) -> bool:
        """持久化是否正常（供 /health 暴露降级）。未启用存储时视为正常。"""
        return self._sessions is None or self._sessions.healthy

    # ------------------------------------------------------------------
    # 人工审批转发（写命令能力为可选；不支持时给出明确语义）
    # ------------------------------------------------------------------
    def pending_approvals(self, *, strict: bool = False) -> list[PendingApproval]:
        """查询已构建 Agent 的申请，不为审批旁路触发 Agent 构造。"""
        result: list[PendingApproval] = []
        for assistant in self._all_assistants():
            if not isinstance(assistant, ApprovalCapable):
                continue
            try:
                result.extend(assistant.pending_approvals())
            except Exception as exc:
                logger.warning("查询待审批项失败: %s", exc)
                if strict:
                    raise ApprovalUnavailableError("Unable to verify pending approvals") from exc
        return sorted(result, key=lambda item: item.created_at, reverse=True)

    def decide(
        self,
        approval_id: str,
        *,
        action: str,
        reason: str = "",
        decided_by: str | None = None,
    ) -> ApprovalOutcome:
        """提交审批并回填原 pending 轮次，处理完成、链式申请和未知结果。

        决策提交后续跑失败不恢复原申请；下一条申请保存失败时取消下一条，避免重放原决策。
        """
        candidates = [
            candidate
            for candidate in self._all_assistants()
            if isinstance(candidate, ApprovalCapable)
        ]
        match = next(
            ((candidate, pending)
             for candidate in candidates
             for pending in candidate.pending_approvals()
             if pending.approval_id == approval_id),
            None,
        )
        assistant = match[0] if match is not None else None
        if match is not None and self._sessions is not None and match[1].session_id:
            try:
                session = self._sessions.get(match[1].session_id)
            except SessionError as exc:
                raise ApprovalUnavailableError("无法核对审批所属会话，请稍后重试") from exc
            if session is None:
                self.cancel_pending(approval_id)
                raise ApprovalError("审批所属会话不存在，已拒绝续跑")
            try:
                saved = self._sessions.has_pending_approval(match[1].session_id, approval_id)
            except SessionError as exc:
                raise ApprovalUnavailableError("无法核对已保存的审批轮次，请稍后重试") from exc
            if not saved:
                raise ApprovalUnavailableError("审批轮次尚未保存或已失效，拒绝续跑")
        # 队列项可能刚过期/被容量淘汰；仍交给一个已启用审批能力的 Agent
        # 产生标准 ApprovalError，随后把会话里的 pending 轮收敛为 abandoned。
        if assistant is None and candidates:
            assistant = candidates[0]
        if assistant is None:
            raise ApprovalUnavailableError(
                "Agent 未启用写命令能力（config/agent_actions.yaml 未启用，或聊天尚未构建 Agent）"
            )
        try:
            if isinstance(assistant, ApprovalDecisionContextCapable):
                outcome = assistant.decide_as(
                    approval_id,
                    action=action,
                    reason=reason,
                    decided_by=decided_by,
                )
            else:
                outcome = assistant.decide(approval_id, action=action, reason=reason)
        except ApprovalUnavailableError:
            raise  # 能力没开 ≠ 这一条失效，不能把它标成 abandoned
        except ApprovalError:
            # 未知 / 已过期 / 已被处理：审批队列里已经没有了，历史里那条不可能再有下文
            self._abandon_pending(approval_id)
            raise
        except Exception as exc:
            try:
                still_pending = any(
                    p.approval_id == approval_id for p in assistant.pending_approvals()
                )
            except Exception:
                still_pending = False
            if still_pending:
                logger.error("Approval resume failed before consumption id=%s: %s", approval_id, exc, exc_info=True)
                raise ApprovalUnavailableError(
                    "Approval is still pending; retry after the temporary failure."
                ) from exc
            self._mark_unknown(approval_id)
            logger.error("Approval resume failed; outcome unknown id=%s: %s", approval_id, exc, exc_info=True)
            raise ApprovalOutcomeUnknownError(
                "Approval was consumed; remote outcome is unknown. Verify the target before retrying."
            ) from exc
        self._resolve_pending(approval_id, outcome)
        return outcome

    def cancel_pending(self, approval_id: str) -> bool:
        """通过已构建 Agent 取消申请；不触发构造，未知或不支持时返回 False。"""
        for assistant in self._all_assistants():
            if not isinstance(assistant, ApprovalCancellable):
                continue
            try:
                if assistant.cancel_pending(approval_id):
                    return True
            except Exception as exc:
                logger.warning("作废待审批项失败 id=%s: %s", approval_id, exc)
        return False

    def _resolve_pending(self, approval_id: str, outcome: ApprovalOutcome) -> None:
        """把审批结果回填到会话表。"""
        if self._sessions is None:
            return
        chained = outcome.status == "pending_approval"
        try:
            updated = self._sessions.update_pending(
                approval_id,
                content=redact_text(outcome.reply),
                status=STATUS_PENDING_APPROVAL if chained else STATUS_FINAL,
                next_approval_id=outcome.next_approval_id,
            )
            if chained and not updated:
                raise SessionError("链式审批的原会话轮次已不存在")
        except SessionError as exc:
            logger.error("回填审批结果失败（历史里该轮将停在待审批态）: %s", exc)
            if chained:
                # 原决策已消费，不能把未持久化的下一条申请交给审批人继续执行。
                if outcome.next_approval_id and not self.cancel_pending(outcome.next_approval_id):
                    logger.error("未保存的链式审批取消失败 id=%s", outcome.next_approval_id)
                self._mark_unknown(approval_id)
                raise ApprovalOutcomeUnknownError(
                    "Approval was consumed, but the next approval could not be saved. "
                    "The next approval cannot be resumed without a saved turn; "
                    "verify execution records before starting again."
                ) from exc

    def _mark_unknown(self, approval_id: str) -> None:
        if self._sessions is None:
            return
        try:
            self._sessions.update_pending(
                approval_id,
                content="Approval resume failed; remote outcome is unknown. Verify the target state.",
                status=STATUS_UNKNOWN,
            )
        except SessionError as exc:
            logger.error("Failed to mark approval outcome unknown id=%s: %s", approval_id, exc)

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

    def _all_assistants(self) -> list[Assistant]:
        result = list(self._host_assistants.values())
        if self._assistant is not None:
            result.append(self._assistant)
        return result

    def _ensure_assistant(self, host_id: str = "") -> Assistant | None:
        """首次聊天时经工厂构建 Agent 并缓存；构建失败标记 broken 后回落。

        **必须持锁 + 双检**：ChatService 是单例，而 FastAPI 的同步路由跑在线程池里，
        两个并发请求会同时看到 `_assistant is None` 各建一个 Agent——那不只是多建一个
        对象：工厂里还会各建一份 `ApprovalStore` 与 `InMemorySaver`，于是**待审批项与
        checkpoint 线程会分散到两个实例里**，表现为「刚批准的命令找不到线程」。
        锁只护首次构建，构建完成后走无锁快路径。
        """
        # 显式注入的单例（测试/兼容扩展点）优先；生产容器不设置它，走下方按主机工厂。
        if self._assistant is not None:
            return self._assistant
        if self._host_assistant_factory is not None:
            if not host_id:
                logger.warning("会话没有主机归属，禁止构建主机 Agent")
                return None
            if host_id in self._host_assistants:
                return self._host_assistants[host_id]
            if host_id in self._host_agent_broken and time.monotonic() < self._host_agent_retry_at.get(host_id, 0):
                return None
            with self._assistant_lock:
                if host_id in self._host_assistants:
                    return self._host_assistants[host_id]
                if host_id in self._host_agent_broken and time.monotonic() < self._host_agent_retry_at.get(host_id, 0):
                    return None
                try:
                    assistant = self._host_assistant_factory(host_id)
                    self._host_assistants[host_id] = assistant
                    self._host_agent_broken.discard(host_id)
                    self._host_agent_retry_at.pop(host_id, None)
                    return assistant
                except Exception as exc:
                    self._host_agent_broken.add(host_id)
                    self._host_agent_retry_at[host_id] = time.monotonic() + _AGENT_RETRY_SECONDS
                    logger.warning(
                        "主机 %s 的 Agent 构建不可用，回落关键词引擎: %s",
                        host_id,
                        exc,
                    )
                    return None
        if self._assistant_factory is None or (self._agent_broken and time.monotonic() < self._agent_retry_at):
            return None
        with self._assistant_lock:
            # 双检：等锁期间可能已被别的线程建好了
            if self._assistant is not None:
                return self._assistant
            if self._agent_broken and time.monotonic() < self._agent_retry_at:
                return None
            try:
                self._assistant = self._assistant_factory()
                self._agent_broken = False
            except Exception as exc:  # 未装 langchain / 模型凭据缺失 / init 失败等
                self._agent_broken = True
                self._agent_retry_at = time.monotonic() + _AGENT_RETRY_SECONDS
                logger.warning("Agent 构建不可用，回落关键词引擎: %s", exc)
            return self._assistant
