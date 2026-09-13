"""interfaces：抽象接口层。

core / api 只依赖这里的抽象基类与异常，不直接依赖具体插件实现；
具体实现位于 src/plugins/，由 src/container.py 装配注入（可替换、可测试）。
"""

from src.interfaces.action import ActionError, ActionExecutor
from src.interfaces.agent_action import (
    AgentActionError,
    AgentActionSource,
    render_agent_action,
)
from src.interfaces.approval import (
    ApprovalError,
    ApprovalOutcome,
    PendingApproval,
    may_decide,
)
from src.interfaces.assistant import (
    ApprovalCancellable,
    ApprovalCapable,
    ApprovalContextCapable,
    Assistant,
)
from src.interfaces.command import CommandSpec, ParamSpec
from src.interfaces.diagnostic import (
    DiagnosticError,
    DiagnosticSource,
    render_diag,
)
from src.interfaces.llm_client import LLMClient, LLMError
from src.interfaces.monitor import MonitorError, MonitorSource
from src.interfaces.session import (
    ROLE_ASSISTANT,
    ROLE_USER,
    STATUS_ABANDONED,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
    ChatMessage,
    HistoryCapable,
    SessionError,
    SessionInfo,
    SessionNotFoundError,
    SessionStore,
    TurnResult,
    is_owner,
)

__all__ = [
    "MonitorSource",
    "MonitorError",
    "ActionExecutor",
    "ActionError",
    "Assistant",
    "ApprovalCapable",
    "ApprovalCancellable",
    "ApprovalContextCapable",
    "ApprovalError",
    "ApprovalOutcome",
    "PendingApproval",
    "may_decide",
    "LLMClient",
    "LLMError",
    "SessionStore",
    "SessionError",
    "SessionNotFoundError",
    "ChatMessage",
    "SessionInfo",
    "TurnResult",
    "HistoryCapable",
    "is_owner",
    "ROLE_USER",
    "ROLE_ASSISTANT",
    "STATUS_FINAL",
    "STATUS_PENDING_APPROVAL",
    "STATUS_ABANDONED",
    "DiagnosticSource",
    "DiagnosticError",
    "AgentActionSource",
    "AgentActionError",
    "CommandSpec",
    "ParamSpec",
    "render_diag",
    "render_agent_action",
]
