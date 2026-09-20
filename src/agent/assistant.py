"""智能运维 Agent 封装层。

职责：
- 使用模型、工具和系统提示词构建聊天 Agent；
- 管理单轮对话和多轮对话；
- 在启用写命令时，通过人工审批中间件在命令执行前中断，批准后继续执行。

设计约束：
- 模型和 LangChain 依赖只在构建 Agent 时惰性加载；
- 中断不是异常，调用方必须先识别待审批状态；
- 审批续跑必须使用同一个 thread_id；
- Agent 不保存历史，多轮历史由会话存储每轮传入。

未安装可选依赖时导入本模块不应报错，构建失败由上层回退到关键词能力。
"""
from __future__ import annotations

import logging
import json
import inspect
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from typing import Sequence

from src.interfaces.agent_action import AgentActionSource
from src.interfaces.approval import (
    ApprovalError,
    ApprovalOutcome,
    ApprovalUnavailableError,
    PendingApproval,
)
from src.interfaces.diagnostic import DiagnosticSource
from src.interfaces.inspection import InspectionStore
from src.interfaces.metric import MetricsConfig
from src.interfaces.monitor import MonitorSource
from src.interfaces.host import MonitorTarget
from src.interfaces.session import ApprovalLedger, ChatMessage, TurnResult
from src.storage.approval import ApprovalStore
from src.utils.model_loader import ModelLoadError, content_text, get_model

logger = logging.getLogger("aiops_assistant.agent")

#: 写命令审批允许的决策（**永不包含 edit**：允许编辑参数等于绕过管理员声明的模板与约束）
ALLOWED_APPROVAL_DECISIONS: tuple[str, ...] = ("approve", "reject")

#: 中断描述前缀（HumanInTheLoopMiddleware 的 description_prefix）
_APPROVAL_PREFIX = "写命令需人工审批"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LLMError(Exception):
    """LLM 初始化 / 调用失败时抛出。"""


# ---------------------------------------------------------------------------
# Agent 构建与封装类
# ---------------------------------------------------------------------------
def _build_agent(
    *,
    model,
    tools: list,
    system_prompt: str,
    name: str,
    response_format=None,
    debug: bool = False,
    interrupt_on: dict | None = None,
    checkpointer=None,
):
    """调用 create_agent 构建一个 LangGraph 智能体（惰性 import langchain）。

    - name 用 AGENT_NAME（日志/trace 归因）；
    - debug 传 create_agent 的 debug 开关（可观测，默认关）；
    - response_format 为结构化输出预留参数（默认 None = 自然语言）；
    - interrupt_on 非空 → 挂 HumanInTheLoopMiddleware（写命令人工审批）。
      **必须同时给 checkpointer**：中断后要靠它按 thread_id 续跑，缺了就没法恢复。
      这里 fail-loud 抛 ValueError——这是编程错误，不该被静默容忍。
    """
    from langchain.agents import create_agent

    kwargs: dict[str, Any] = {}
    if interrupt_on:
        if checkpointer is None:
            raise ValueError(
                "配置了写命令审批（interrupt_on）但没有 checkpointer："
                "中断后无法按 thread_id 续跑，写命令将永远挂起"
            )
        # 惰性导入：只在真的启用写命令审批时才引入中间件
        from langchain.agents.middleware import HumanInTheLoopMiddleware

        kwargs["middleware"] = [
            HumanInTheLoopMiddleware(
                interrupt_on=dict(interrupt_on),
                description_prefix=_APPROVAL_PREFIX,
            )
        ]
        kwargs["checkpointer"] = checkpointer

    agent = create_agent(
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        name=name,
        debug=debug,
        response_format=response_format,
        **kwargs,
    )
    logger.debug(
        "Agent 构建完成 name=%s tools=%d 审批工具=%d",
        name, len(tools), len(interrupt_on or {}),
    )
    return agent


def _extract_interrupt(response: Any) -> dict | None:
    """从 invoke 结果里取出中断载荷；无中断返回 None。

    中断**不是异常**：invoke 正常返回，只是多带一个 "__interrupt__" 键。
    该键下是 Interrupt 对象列表，其 .value 形如
    {"action_requests": [...], "review_configs": [...]}。
    """
    if not isinstance(response, dict):
        return None
    interrupts = response.get("__interrupt__") or []
    if not interrupts:
        return None
    value = getattr(interrupts[0], "value", None)
    return value if isinstance(value, dict) else {}


def _render_pending(
    approval_id: str, interrupt_value: dict, *, target_summary: str = ""
) -> str:
    """把待审批项渲染成给用户看的文本（同时也是 /chat 的 reply 内容）。

    文案要点：说清是什么命令、什么参数、命令**尚未执行**、以及怎么批准。
    """
    actions = interrupt_value.get("action_requests") or []
    lines = [f"⚠️ 待人工审批（审批通过前命令不会执行）｜审批 id：{approval_id}"]
    if target_summary:
        lines.append(f"  · 目标主机：{target_summary}")
    for item in actions:
        name = item.get("name", "?")
        args = item.get("args") or {}
        detail = "、".join(f"{k}={v}" for k, v in args.items()) or "（无参数）"
        lines.append(f"  · {name}（{detail}）")
    lines.append(
        f"批准：POST /approvals/{approval_id} {{\"action\": \"approve\"}}；"
        f"拒绝：{{\"action\": \"reject\"}}"
    )
    return "\n".join(lines)


def _last_ai_content(response: dict[str, Any]) -> str:
    """取**本轮**最后一条有内容的 AI 消息文本。

    **反向扫必须止步于本轮的用户消息。** `response["messages"]` 是**整条线程的累积
    state**（含历史——已实测：输入 3 条 → 返回 4 条，且首条就是输入首条），无脑扫全部
    消息时，本轮 AI 的 content 若为空（模型没给文本 / 中断轮），就会捞到**上一轮的旧回答**
    返回给用户——静默串味，比返回兜底文案更糟，因为它看起来像个正常答案。
    回归见 tests/test_agent_actions_hitl_graph.py::test_last_ai_content_ignores_history。

    止步于 human 而不是「跳过前 N 条」：decide() 的续跑路径拿不到「本轮输入条数」
    （resume 不新增用户消息，response 是整条线程 state），而「最后一条 human 之后」
    在多轮、续跑、中断重入三种情形下都恰好是本轮产出。
    """
    for msg in reversed(response.get("messages", [])):
        if getattr(msg, "type", "") == "human":
            break  # 本轮起点：再往前都是历史
        # msg.type == "ai" 且 content 非空才算最终回答（工具调用轮 content 常为空）
        if getattr(msg, "type", "") == "ai" and msg.content:
            return content_text(msg.content)
    return "抱歉，我暂时无法回答这个问题。"


class OpsAssistant:
    """智能运维 Agent 封装类。

    对话入口：
    - reply()：单轮对话；
    - reply_turn(history=...)：带历史的一轮对话。

    多轮历史由调用方每轮从会话表读取后整段传入；checkpointer 只保存审批中断的临时状态，
    本类不持有对话历史。
    """

    def __init__(
        self,
        monitor: MonitorSource,
        *,
        metrics_config: MetricsConfig | None = None,
        inspection_store: InspectionStore | None = None,
        diag: DiagnosticSource | None = None,
        agent_actions: AgentActionSource | None = None,
        approvals: ApprovalStore | None = None,
        ledger: ApprovalLedger | None = None,
        name: str = "ops_assistant",
        system_prompt: str | None = None,
        response_format=None,
        debug: bool = False,
        recursion_limit: int = 25,
        host_id: str = "",
        monitor_target: MonitorTarget | None = None,
        target_summary: str = "",
        config_fingerprint: str = "",
    ) -> None:
        # 本构造会触发 langchain / init.py 惰性导入；失败向上抛，由调用方回落
        from src.agent.prompts import (
            AGENT_NAME,
            OPS_SYSTEM_PROMPT,
            WRITE_TOOLS_ADDENDUM,
        )
        from src.agent.tools import (
            build_agent_action_tools,
            build_diag_tools,
            build_inspection_tools,
            build_tools,
        )

        if monitor is None:
            raise ValueError("OpsAssistant 需要注入 monitor（MonitorSource 实现）")
        self.monitor = monitor
        self.diag = diag
        self.agent_actions = agent_actions
        self.name = name or AGENT_NAME
        self.system_prompt = system_prompt or OPS_SYSTEM_PROMPT
        self.debug = debug
        self.recursion_limit = max(int(recursion_limit), 1)
        self.host_id = host_id
        self.target_summary = target_summary or host_id
        self.config_fingerprint = config_fingerprint
        self.checkpointer = None

        # 监控工具 + 只读诊断工具（诊断源为 None 时不注册，Agent 看不到该能力）
        self.tools = build_tools(monitor, metrics_config, target=monitor_target)
        self.tools = [
            *self.tools,
            *build_inspection_tools(inspection_store, host_id=host_id or None),
        ]
        if diag is not None:
            self.tools = [*self.tools, *build_diag_tools(diag)]

        # 写命令工具 + 审批策略（成对产出）。reserved = 已被上面占用的工具名，
        # 冲突的命令会被丢弃并记 ERROR（重名工具在 create_agent 里是静默遮蔽的）。
        bundle = build_agent_action_tools(
            agent_actions, reserved={t.name for t in self.tools}
        )
        self.write_tools = bundle.tools
        self.interrupt_on = bundle.interrupt_on

        self.approvals = approvals
        #: 审批决策落盘 / 淘汰后对账的口（窄接口，见 interfaces.session.ApprovalLedger）。
        #: 只在有写命令能力时才有意义；持久化未启用时为 None，那些机制整体跳过。
        self.ledger = ledger if self.write_tools else None
        if self.write_tools:
            if approvals is None:
                raise ValueError(
                    "存在 Agent 写命令工具但没有注入 approval store："
                    "写命令必须可审批，缺少 store 会使其无法恢复"
                )
            # 中断续跑依赖 checkpointer（惰性导入，仅在有写命令时才引入）
            from langgraph.checkpoint.memory import InMemorySaver

            self.checkpointer = InMemorySaver()
            self.tools = [*self.tools, *self.write_tools]
            # 只有真的注册了写工具，才告诉模型它有写能力（否则它会谎称能重启服务）
            self.system_prompt = self.system_prompt + WRITE_TOOLS_ADDENDUM

        # 惰性复用 init.py 模型入口（utils/model_loader 单例缓存）；模型/provider 只改 init.py
        try:
            self.model = get_model()
        except ModelLoadError as exc:
            raise LLMError(str(exc)) from exc
        self.agent = _build_agent(
            model=self.model,
            tools=self.tools,
            system_prompt=self.system_prompt,
            name=self.name,
            response_format=response_format,
            debug=debug,
            interrupt_on=self.interrupt_on,
            checkpointer=self.checkpointer,
        )
    # -- 对话（单轮 / 带历史） ---------------------------------------------
    def reply(self, message: str) -> str:
        """单轮对话（Assistant 契约）：等价于历史为空的一轮。"""
        return self.reply_turn(message).text

    def reply_turn(
        self, message: str, *, history: Sequence[ChatMessage] = ()
    ) -> TurnResult:
        """带历史跑一轮（HistoryCapable 契约）。

        **只是 `reply_turn_ctx` 不带上下文的简化形式**——实现只有一条路径，
        照 `reply` → `reply_turn` 的先例。既有调用方（含各测试假对象）无需改动。
        """
        return self.reply_turn_ctx(message, history=history)

    def reply_turn_ctx(
        self,
        message: str,
        *,
        history: Sequence[ChatMessage] = (),
        session_id: str | None = None,
        user: str | None = None,
    ) -> TurnResult:
        """带历史 + **会话上下文**跑一轮（ApprovalContextCapable 契约）。

        history 是**可进模型窗口**的历史消息（只有 final 状态），由调用方从会话表取出；
        本方法**不读写任何存储**——持久化是 core 的事，Agent 只负责这一轮怎么跑。

        模型可自主决定是否调用监控工具（cpu_usage 等）后组织回答。

        有写命令时：本轮若触发写命令会**中断**，返回 status="pending_approval" 与
        approval_id，命令尚未执行；待人工经 /approvals 决策。

        `session_id` / `user` **只用于登记待审批项的归属**（`/approvals` 要按身份隔离、
        `approval_decisions` 表要记会话），不影响这一轮怎么跑、也不进模型上下文。

        **每轮仍是全新的 thread_id**：历史由调用方每轮重新拼进来，
        checkpointer 只当「审批中断的临时草稿纸」，跑完即删——已实测：新 thread +
        完整历史不会跨轮累积。
        """
        if not (message or "").strip():
            raise LLMError("消息为空")
        logger.info("Agent 收到消息: %s（历史 %d 条）", message, len(history))

        # 有 checkpointer 就必须带 thread_id：否则 create_agent 会抛 ValueError，
        # 被 ChatService 的 catch-all 吞掉 → 整条聊天静默回落关键词。
        thread_id = uuid4().hex if self.checkpointer is not None else None
        self._purge_expired()

        payload = [{"role": m.role, "content": m.content} for m in history]
        payload.append({"role": "user", "content": message})

        config = (
            {"configurable": {"thread_id": thread_id}, "recursion_limit": self.recursion_limit}
            if thread_id
            else {"recursion_limit": self.recursion_limit}
        )
        response = self.agent.invoke({"messages": payload}, config=config)

        # **先判中断**：中断那轮的 AI 消息 content 为空，若先取文本会返回
        # 「抱歉，我暂时无法回答这个问题。」，把待审批伪装成失败。
        interrupted = _extract_interrupt(response)
        if interrupted is not None:
            approval_id = self._register_pending(
                thread_id, interrupted, session_id=session_id, user=user
            )
            return TurnResult(
                text=_render_pending(
                    approval_id, interrupted, target_summary=self.target_summary
                ),
                status="pending_approval",
                approval_id=approval_id,
            )

        try:
            return TurnResult(text=_last_ai_content(response))
        finally:
            # 本轮已跑完，线程不再需要（不删会让 InMemorySaver 随请求数无限增长）
            self._delete_thread(thread_id)

    def cancel_pending(self, approval_id: str) -> bool:
        """作废一条待审批项并清掉其线程（ApprovalCancellable 契约）。

        用于会话被删除时：宁可让人重新发起，也不能让一个陈旧的写命令还挂在队列里等执行。
        返回是否命中（未知 / 已过期 / 已被处理 → False）。
        """
        if self.approvals is None:
            return False
        record = self.approvals.remove(approval_id)
        if record is None:
            return False
        self._delete_thread(record.get("thread_id"))
        logger.warning("待审批项被作废 id=%s（其命令不会执行）", approval_id)
        return True

    # -- 人工审批（ApprovalCapable） ---------------------------------------
    def pending_approvals(self) -> list[PendingApproval]:
        """当前待人工决策的写命令调用（新的在前）。无写命令能力时恒为空。"""
        if self.approvals is None:
            return []
        return [
            PendingApproval(
                approval_id=rec["approval_id"],
                created_at=rec.get("created_at", ""),
                actions=tuple(rec.get("actions") or ()),
                allowed_decisions=tuple(
                    rec.get("allowed_decisions") or ALLOWED_APPROVAL_DECISIONS
                ),
                session_id=rec.get("session_id"),
                user=rec.get("user") or "",
                host_id=rec.get("host_id") or "",
                target_summary=rec.get("target_summary") or "",
                config_fingerprint=rec.get("config_fingerprint") or "",
            )
            for rec in self.approvals.list()
        ]

    def decide(self, approval_id: str, *, action: str, reason: str = "") -> ApprovalOutcome:
        """提交一次审批决策并续跑：approve → 真正执行；reject → 放弃。

        严格性说明（都对应实测过的坑）：
        - **单次性**：先 pop 再续跑，成功进入续跑后重复提交同一 id 不会二次执行；
          若决策记录在续跑前明确落盘失败，则恢复审批项，允许同一 id 安全重试；
        - **先校验后 resume**：决策类型必须在 allowed_decisions 内、且决策条数必须等于挂起的
          工具调用数。负载不合法时 LangGraph 会把该负载缓存为线程的 resume 值 →
          **线程被永久毒化**，之后连正确负载也执行不了，因此绝不能让错误负载进入 resume。
        """
        if self.approvals is None:
            raise ApprovalError("当前 Agent 未启用写命令能力，无法审批")
        if action not in ALLOWED_APPROVAL_DECISIONS:
            raise ApprovalError(
                f"不支持的审批决策 {action!r}，仅支持: {', '.join(ALLOWED_APPROVAL_DECISIONS)}"
            )

        record = self.approvals.pop(approval_id)  # 先取走 → 天然防重放
        if record is None:
            raise ApprovalError(f"未知、已过期或已被处理的审批 id: {approval_id}")

        actions = list(record.get("actions") or ())
        if (
            record.get("host_id") != self.host_id
            or record.get("config_fingerprint", "") != self.config_fingerprint
        ):
            self._delete_thread(record.get("thread_id"))
            raise ApprovalError(
                "待审批项的主机或配置已变化，已拒绝执行；请在当前配置下重新发起"
            )
        allowed = tuple(record.get("allowed_decisions") or ALLOWED_APPROVAL_DECISIONS)
        if action not in allowed:
            raise ApprovalError(
                f"该审批项不允许的决策 {action!r}，可选: {', '.join(allowed)}"
            )

        thread_id = record.get("thread_id")
        if not thread_id:
            raise ApprovalError(f"审批项 {approval_id} 缺少线程信息，无法续跑")

        # ★ 落决策记录。**位置就是这套机制的全部要害**：必须在 pop() 之后、resume() 之前。
        #   进程若恰好死在这两者之间，重启时 reconcile_pending 才能把那一轮标成
        #   「结果未知」而不是错误的「未执行」——命令可能已经执行过了。
        # 落盘明确失败时命令尚未开始执行，可以安全恢复原审批项供调用方重试。
        # resume() 失败时绝不能恢复：那时远端命令可能已经执行，重试会造成重复写。
        try:
            self._record_decision(approval_id, record, action)
        except ApprovalUnavailableError:
            evicted = self.approvals.add(record)
            if evicted is not None and evicted.get("approval_id") != approval_id:
                self._delete_thread(evicted.get("thread_id"))
                self._reconcile_ledger()
            logger.warning(
                "审批决策记录失败，待审批项已恢复，可重试 id=%s", approval_id
            )
            raise

        # 决策顺序与条数必须与挂起调用一一对应（多了少了都会毒化线程）
        decisions = [{"type": action} for _ in actions]
        reason_note = f"，理由: {reason}" if reason else ""
        logger.info(
            "人工审批 id=%s 决策=%s 工具=%s%s",
            approval_id, action, [a.get("name") for a in actions], reason_note,
        )

        from langgraph.types import Command  # 惰性导入：仅审批路径需要

        if action == "reject" and hasattr(self.agent_actions, "record_rejection"):
            self.agent_actions.record_rejection(approval_id, actions)
        execution_context = (
            self.agent_actions.approval_context(approval_id)
            if action == "approve"
            and self.agent_actions is not None
            and hasattr(self.agent_actions, "approval_context")
            else nullcontext()
        )

        with execution_context:
            response = self.agent.invoke(
                Command(resume={"decisions": decisions}),
                config={
                    "configurable": {"thread_id": thread_id},
                    "recursion_limit": self.recursion_limit,
                },
            )

        # 续跑可能触发**下一条**写命令（同一线程、新的中断），需继续挂起等审批
        interrupted = _extract_interrupt(response)
        if interrupted is not None:
            # 链式审批也属于**同一个会话、同一个触发者**，上下文原样继承——
            # 否则第二条待审批项会变成「无主」，连原主都批不了。
            next_id = self._register_pending(
                thread_id,
                interrupted,
                session_id=record.get("session_id"),
                user=record.get("user"),
            )
            return ApprovalOutcome(
                approval_id=approval_id,
                decision=action,
                status="pending_approval",
                reply=_render_pending(
                    next_id, interrupted, target_summary=self.target_summary
                ),
                next_approval_id=next_id,
            )

        try:
            return ApprovalOutcome(
                approval_id=approval_id,
                decision=action,
                status="completed",
                reply=_last_ai_content(response),
            )
        finally:
            self._delete_thread(thread_id)

    # -- 内部：中断登记与线程清理 ------------------------------------------
    def _register_pending(
        self,
        thread_id: str | None,
        interrupt_value: dict,
        *,
        session_id: str | None = None,
        user: str | None = None,
    ) -> str:
        """把一次中断登记为待审批项，返回审批 id。

        `session_id` / `user` 是**这条待审批的归属**：`/approvals` 靠它做身份隔离，
        `approval_decisions` 表靠它记会话。两者都由调用方（core 层）送进来——
        Agent 自己既不知道当前是哪个会话，也不知道调用者是谁。
        """
        if self.approvals is None or thread_id is None:
            raise ApprovalError("写命令中断但缺少审批存储或线程信息，无法登记")
        approval_id = uuid4().hex[:12]
        actions = tuple(interrupt_value.get("action_requests") or ())
        # add() 的返回值是**被容量上限淘汰掉的那条**（没淘汰则为 None）。
        # 它再也不可能被续跑了，必须连同其 checkpoint 线程一起清掉——否则 InMemorySaver
        # 会随中断次数无限增长（线程泄漏）。这个返回值就是为调用方清理而设计的。
        evicted = self.approvals.add(
            {
                "approval_id": approval_id,
                "thread_id": thread_id,
                "actions": actions,
                "allowed_decisions": ALLOWED_APPROVAL_DECISIONS,
                "created_at": _now_iso(),
                "session_id": session_id,
                "user": user or "",
                "host_id": self.host_id,
                "target_summary": self.target_summary,
                "config_fingerprint": self.config_fingerprint,
            }
        )
        if evicted is not None:
            self._delete_thread(evicted.get("thread_id"))
            logger.warning(
                "待审批队列已满，淘汰最旧一条 id=%s（其命令将永不执行）",
                evicted.get("approval_id"),
            )
            # 容量淘汰也是一次「队列变了」——同样要收敛会话里那几轮，
            # 否则它们会永远显示「有一条命令等你批」。
            self._reconcile_ledger()
        logger.warning(
            "写命令待人工审批 id=%s 工具=%s（未批准前不会执行）",
            approval_id, [a.get("name") for a in actions],
        )
        return approval_id

    def _record_decision(self, approval_id: str, record: dict, action: str) -> None:
        """落一条决策记录（在 `pop()` 与 `resume()` 之间调用）。

        **写失败就中止这次决策**（抛 `ApprovalUnavailableError` → api 503）。调用方会在
        `decide()` 中把审批项恢复到队列，供同一 id 重试；恢复是安全的，因为失败发生在
        resume 之前，命令确定尚未执行。反过来若吞掉错误继续执行，一旦进程随后崩溃，
        重启后就会得到「以为没执行、其实执行了」的错误审计结论。

        `ledger` 为 None（持久化未启用）时直接跳过：没有会话表，也就没有要分辨的东西。
        """
        if self.ledger is None:
            return
        try:
            method = self.ledger.record_decision
            kwargs = {
                "session_id": record.get("session_id"),
                "action": action,
                "decided_by": record.get("user") or "",
            }
            signature = inspect.signature(method)
            supports_extended = (
                "host_id" in signature.parameters
                or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in signature.parameters.values()
                )
            )
            if supports_extended:
                kwargs.update(
                    host_id=record.get("host_id") or "",
                    target_summary=record.get("target_summary") or "",
                    actions_json=json.dumps(
                        record.get("actions") or (),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    config_fingerprint=record.get("config_fingerprint") or "",
                )
            method(approval_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 一律转成「审批暂不可用」，别炸成 500
            logger.error("审批决策记录落盘失败，已中止本次决策（命令未执行）: %s", exc)
            raise ApprovalUnavailableError(
                f"无法记录本次审批决策（存储故障），已中止且未执行命令，请重试: {exc}"
            ) from exc

    def _purge_expired(self) -> None:
        """清理过期待审批项，并连同其线程一起删除（否则线程只增不减）。

        **真淘汰了才去对账**：没淘汰时（绝大多数的轮次）一点额外开销都没有。
        （另一条淘汰路径是**容量**上限，那条发生在 `add()` 里、由 `_register_pending`
        处理——两条路径都要对账，漏一条就会留下永远批不掉的「待审批」行。）
        """
        if self.approvals is None:
            return
        purged = self.approvals.purge_expired()
        if not purged:
            return
        for rec in purged:
            self._delete_thread(rec.get("thread_id"))
        self._reconcile_ledger()

    def _reconcile_ledger(self) -> None:
        """把「审批队列里已经没有了的」会话轮收敛掉。

        队列刚变过（TTL 过期 / 容量淘汰）时调用。不收敛的话那些行会一直显示
        「有一条命令等你批」，而 `/approvals` 里根本没有它——用户永远批不了，
        只能等下次重启。
        """
        if self.ledger is None or self.approvals is None:
            return
        try:
            live = {r["approval_id"] for r in self.approvals.list()}
            self.ledger.reconcile_pending(live)
        except Exception as exc:  # noqa: BLE001 - 收敛失败不该影响本轮对话
            logger.warning("待审批项变化后对账失败（不影响本轮）: %s", exc)

    def _delete_thread(self, thread_id: str | None) -> None:
        """删除已完结/已作废的线程（checkpointer 无自动回收）。"""
        if not thread_id or self.checkpointer is None:
            return
        try:
            self.checkpointer.delete_thread(thread_id)
        except Exception as exc:  # pragma: no cover - 清理失败不应影响主流程
            logger.debug("删除线程 %s 失败: %s", thread_id, exc)

    # Agent 是单例，不保存实例级对话历史；多轮历史由 reply_turn(history=...) 传入。
