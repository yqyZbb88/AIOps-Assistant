"""Assistant：聊天 Agent 契约（结构化 Protocol）。

core/chat.py 的 ChatService 只需「给一条消息、拿回一段文本」，
不感知 langchain / langgraph，也不感知模型如何构建——这正是本契约存在的意义：
- 生产实现见 src/agent/assistant.py::OpsAssistant（惰性构建 langchain Agent）；
- 测试可注入任意同形状假对象（见 tests/test_chat.py 的 FakeAssistant），无需装 langchain。

用 Protocol 而非 ABC：实现方（agent 层）不必知道自己实现了本接口，
也避免 core → agent 的反向 import。typing.Protocol 是标准库。
"""
from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from src.interfaces.approval import ApprovalOutcome, PendingApproval
from src.interfaces.session import ChatMessage, TurnResult


class Assistant(Protocol):
    """聊天 Agent：单轮对话，返回最终回答文本。"""

    def reply(self, message: str) -> str:
        """把用户消息交给 Agent，返回回答文本；失败时抛异常由调用方回落。"""
        ...


@runtime_checkable
class ApprovalCapable(Protocol):
    """可审批的 Agent（可选能力）：支持列出待审批项与提交人工决策。

    **刻意不并入 Assistant**：把方法加进 Assistant 会让所有既有实现/假对象
    （如 tests 里只有 reply 的 FakeAssistant）结构性失效，且「能不能审批」
    本就是能力探测而非必经契约。调用方用 isinstance(assistant, ApprovalCapable)
    守卫——这也是它标 runtime_checkable 的原因。

    只读 Agent（无写命令目录）不实现本协议，属正常状态：
    core/chat.py 对不支持者返回空列表 / 抛 ApprovalError。
    """

    def pending_approvals(self) -> list[PendingApproval]:
        """列出当前待人工决策的写命令调用（新的在前）。"""
        ...

    def decide(self, approval_id: str, *, action: str, reason: str = "") -> ApprovalOutcome:
        """提交一次审批决策（approve/reject）并续跑；未知/过期/已用过抛 ApprovalError。"""
        ...


@runtime_checkable
class ApprovalCancellable(Protocol):
    """可**作废**待审批项的 Agent（可选能力）。

    用于「会话被删除 → 该会话的未决审批一并作废」：宁可让人重新发起，也不能让一个
    陈旧的写命令还挂在队列里等着被执行（继承 M3 的 fail-safe 取向）。

    **刻意另立协议而不往 `ApprovalCapable` 里加方法**：它是 runtime_checkable 的
    结构化协议，加方法会让所有既有实现（如 tests 的 FakeApprovalAssistant）**静默地
    不再 isinstance 通过**，从而整体降级成「审批不可用」——这类失败不报错，只表现为
    功能悄悄消失。要加新能力就照这个先例新立一个协议。
    """

    def cancel_pending(self, approval_id: str) -> bool:
        """作废一条待审批项（其命令永不执行）；命中返回 True，未知/已处理返回 False。"""
        ...


@runtime_checkable
class ApprovalContextCapable(Protocol):
    """跑一轮时**顺便记下上下文**的 Agent（可选能力）。

    与 `HistoryCapable` 只差一点：多接收 `session_id` 与 `user`，并把它们记进这一轮
    触发的待审批项。为什么需要——

    - `/approvals` 要能**按身份隔离**：否则任何持有效密钥的人都能列出并批准**所有人**
      的待执行写命令；
    - `approval_decisions` 表要记 `session_id`，没有它那张表根本写不进去。

    两件事要的是同一样东西：**「这条待审批是谁、在哪个会话里触发的」必须是审批记录
    自身的属性**，而不能靠外部关联去反推（反推不出时只剩 fail-open 或 fail-closed
    两个坏选择）。

    **为什么不给 `HistoryCapable.reply_turn` 直接加 kwarg**：`runtime_checkable` 只校验
    方法名、不校验签名，给既有方法加参数会让只实现旧签名的对象被**误判为支持**，然后
    在调用处 TypeError——这与 `ApprovalCancellable` 另立协议是同一条理由。
    """

    def reply_turn_ctx(
        self,
        message: str,
        *,
        history: Sequence[ChatMessage] = (),
        session_id: str | None = None,
        user: str | None = None,
    ) -> TurnResult:
        """同 `HistoryCapable.reply_turn`，额外把 `session_id` / `user` 记进待审批项。"""
        ...
