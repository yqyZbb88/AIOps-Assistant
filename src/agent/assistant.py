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
    """提取本轮最后一条非空 AI 文本，反向扫描止于最近 human 消息。

    避免本轮空输出时误用历史回答；resume 不新增 human，同一边界仍适用于审批续跑。
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
        model_factory=None,
        debug: bool = False,
        recursion_limit: int = 25,
        host_id: str = "",
        monitor_target: MonitorTarget | None = None,
        target_summary: str = "",
        config_fingerprint: str = "",
    ) -> None:
        # 本构造会触发 模型 SDK 惰性导入；失败向上抛，由调用方回落
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

        # 惰性复用通用模型入口；配置版本由运行快照固定
        try:
            self.model = model_factory() if model_factory is not None else get_model()
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
        """不带会话上下文的一轮调用，统一委托 reply_turn_ctx。"""
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
        聊天正文由 core 落库；审批过期清理和队列淘汰会通过注入的 ledger 对账。

        模型可自主决定是否调用监控工具（cpu_usage 等）后组织回答。

        有写命令时：本轮若触发写命令会**中断**，返回 status="pending_approval" 与
        approval_id，命令尚未执行；待人工经 /approvals 决策。

        `session_id` / `user` **只用于登记待审批项的归属**（`/approvals` 要按身份隔离、
        `approval_decisions` 表要记会话），不影响这一轮怎么跑、也不进模型上下文。

        **每轮仍是全新的 thread_id**：历史由调用方每轮重新拼进来，
        checkpointer 只保留审批中断的临时状态，最终回复或运行失败后删除线程。
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
        retain_thread = False
        try:
            response = self.agent.invoke({"messages": payload}, config=config)

            # **先判中断**：中断那轮的 AI 消息 content 为空，若先取文本会返回
            # 「抱歉，我暂时无法回答这个问题。」，把待审批伪装成失败。
            interrupted = _extract_interrupt(response)
            if interrupted is not None:
                approval_id = self._register_pending(
                    thread_id, interrupted, session_id=session_id, user=user
                )
                retain_thread = True
                return TurnResult(
                    text=_render_pending(
                        approval_id, interrupted, target_summary=self.target_summary
                    ),
                    status="pending_approval",
                    approval_id=approval_id,
                )

            return TurnResult(text=_last_ai_content(response))
        finally:
            if not retain_thread:
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
        self._terminal_request(approval_id, "cancelled")
        self._delete_thread(record.get("thread_id"))
        logger.warning("待审批项被作废 id=%s（其命令不会执行）", approval_id)
        return True

    # -- 人工审批（ApprovalCapable） ---------------------------------------
    def pending_approvals(self) -> list[PendingApproval]:
        """当前待人工决策的写命令调用（新的在前）。无写命令能力时恒为空。"""
        if self.approvals is None:
            return []
        self._purge_expired()
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
        """兼容旧调用方：未显式提供决策人时沿用申请人身份。"""
        return self._decide(
            approval_id,
            action=action,
            reason=reason,
            decided_by=None,
        )

    def decide_as(
        self,
        approval_id: str,
        *,
        action: str,
        reason: str = "",
        decided_by: str | None = None,
    ) -> ApprovalOutcome:
        """提交审批并把 API 已认证的真实决策人写入持久化审计。"""
        return self._decide(
            approval_id,
            action=action,
            reason=reason,
            decided_by=decided_by,
        )

    def _decide(
        self,
        approval_id: str,
        *,
        action: str,
        reason: str = "",
        decided_by: str | None = None,
    ) -> ApprovalOutcome:
        """原子消费审批、先持久决策再恢复线程，批准或拒绝冻结动作。

        决策参数必须在 resume 前校验，避免非法载荷污染 checkpoint。决策落库失败
        可重新入队，进入续跑后失败则结果不确定，不恢复原申请。
        """
        if self.approvals is None:
            raise ApprovalError("当前 Agent 未启用写命令能力，无法审批")
        if action not in ALLOWED_APPROVAL_DECISIONS:
            raise ApprovalError(
                f"不支持的审批决策 {action!r}，仅支持: {', '.join(ALLOWED_APPROVAL_DECISIONS)}"
            )

        # 先拿到到期记录并释放线程；pop 自行清理时调用方拿不到 thread_id。
        self._purge_expired()
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
            self._record_decision(
                approval_id,
                record,
                action,
                decided_by=decided_by,
            )
        except ApprovalUnavailableError:
            evicted = self.approvals.add(record)
            if evicted is not None and evicted.get("approval_id") != approval_id:
                self._terminal_request(evicted.get("approval_id"), "evicted")
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

        # 决策已消费后，异常也必须释放检查点；只保留成功登记的链式审批。
        retain_thread = False
        try:
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
                retain_thread = True
                return ApprovalOutcome(
                    approval_id=approval_id,
                    decision=action,
                    status="pending_approval",
                    reply=_render_pending(
                        next_id, interrupted, target_summary=self.target_summary
                    ),
                    next_approval_id=next_id,
                )

            return ApprovalOutcome(
                approval_id=approval_id,
                decision=action,
                status="completed",
                reply=_last_ai_content(response),
            )
        finally:
            if not retain_thread:
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
        """登记冻结动作及调用方提供的会话/身份；容量淘汰同时释放线程并对账。"""
        if self.approvals is None or thread_id is None:
            raise ApprovalError("写命令中断但缺少审批存储或线程信息，无法登记")
        approval_id = uuid4().hex[:12]
        actions = tuple(interrupt_value.get("action_requests") or ())
        # add() 的返回值是**被容量上限淘汰掉的那条**（没淘汰则为 None）。
        # 它再也不可能被续跑了，必须连同其 checkpoint 线程一起清掉——否则 InMemorySaver
        # 会随中断次数无限增长（线程泄漏）。这个返回值就是为调用方清理而设计的。
        record = {
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
        if self.ledger is not None and hasattr(self.ledger, "record_request"):
            self.ledger.record_request(record)
        evicted = self.approvals.add(record)
        if evicted is not None:
            self._terminal_request(evicted.get("approval_id"), "evicted")
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

    def _record_decision(
        self,
        approval_id: str,
        record: dict,
        action: str,
        *,
        decided_by: str | None = None,
    ) -> None:
        """在 pop 与 resume 之间记录决策；失败上抛，调用方重新入队。

        只有恢复执行之前的失败可安全重试当前申请；无 ledger 的内部调用跳过持久化。
        """
        if self.ledger is None:
            return
        try:
            method = self.ledger.record_decision
            kwargs = {
                "session_id": record.get("session_id"),
                "action": action,
                # API 调用必须使用已认证的审批人；None 仅用于可信内部旧入口，
                # 此时沿用申请人以保持既有直接调用语义。
                "decided_by": (
                    decided_by
                    if decided_by is not None
                    else record.get("user") or ""
                ),
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
        """清理过期申请及 checkpoint；实际淘汰后收敛会话 pending 轮次。"""
        if self.approvals is None:
            return
        purged = self.approvals.purge_expired()
        if not purged:
            return
        for rec in purged:
            self._terminal_request(rec.get("approval_id"), "expired")
            self._delete_thread(rec.get("thread_id"))
        self._reconcile_ledger()

    def _terminal_request(self, approval_id, status):
        if self.ledger is not None and hasattr(self.ledger, "terminal_request"):
            self.ledger.terminal_request(approval_id, status)

    def _reconcile_ledger(self) -> None:
        """在审批过期或容量淘汰后收敛会话，避免留下不可处理的 pending。"""
        if self.ledger is None or self.approvals is None:
            return
        try:
            live = {r["approval_id"] for r in self.approvals.list()}
            method = self.ledger.reconcile_pending
            # 生产账本共享给多个主机；内部旧注入实现保留兼容调用。
            if self.host_id and "host_id" in inspect.signature(method).parameters:
                method(live, host_id=self.host_id)
            else:
                method(live)
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
