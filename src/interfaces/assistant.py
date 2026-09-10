"""Assistant：聊天 Agent 契约（结构化 Protocol）。

core/chat.py 的 ChatService 只需「给一条消息、拿回一段文本」，
不感知 langchain / langgraph，也不感知模型如何构建——这正是本契约存在的意义：
- 生产实现见 src/agent/assistant.py::OpsAssistant（惰性构建 langchain Agent）；
- 测试可注入任意同形状假对象（见 tests/test_chat.py 的 FakeAssistant），无需装 langchain。

用 Protocol 而非 ABC：实现方（agent 层）不必知道自己实现了本接口，
也避免 core → agent 的反向 import。typing.Protocol 是标准库。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from src.interfaces.approval import ApprovalOutcome, PendingApproval


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
