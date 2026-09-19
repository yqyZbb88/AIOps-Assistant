"""人工审批契约：待审批项与审批结果的中性类型（无第三方依赖）。

Agent 写命令在真正执行前会**中断**（LangGraph interrupt），等待人工决策。
本模块描述「中断后暴露给外界待审批项」与「决策后返回的结果」的形状，
供 core / api 层使用——它们都不感知 langchain/langgraph。

安全约定：
- PendingApproval 只含**摘要**（命令名 + 语义参数 + 说明），**绝不含连接凭据**；
- allowed_decisions 恒为 approve/reject，**永不包含 edit**——允许编辑等于绕过
  管理员在 config/agent_actions.yaml 里声明的命令模板与参数约束。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Collection


class ApprovalError(Exception):
    """审批失败（未知 / 已过期 / 已用过的审批 id）。"""


class ApprovalUnavailableError(ApprovalError):
    """审批能力不可用（未启用写命令目录，或 Agent 尚未构建）。

    与「id 不存在」区分开：这是**功能没开**而非**找错了 id**，
    api 层据此返回 503 而非 404——两者要运维处理的事情完全不同。
    """


@dataclass(frozen=True)
class PendingApproval:
    """一个待人工决策的写命令调用（中断态）。

    actions 是 LangGraph 中断载荷里的 action_requests（已剔除凭据），形如
    [{"name": "restart_service", "args": {"service": "nginx"}, "description": "..."}]。
    """

    approval_id: str
    created_at: str
    actions: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    allowed_decisions: tuple[str, ...] = ("approve", "reject")
    #: 触发这条写命令的**身份**（由密钥推出）；空串 = 鉴权关闭时产生的
    user: str = ""
    host_id: str = ""
    target_summary: str = ""
    config_fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为 api 层直接可序列化的结构。"""
        return {
            "approval_id": self.approval_id,
            "created_at": self.created_at,
            "actions": list(self.actions),
            "allowed_decisions": list(self.allowed_decisions),
            "user": self.user,
            "host_id": self.host_id or None,
            "target_summary": self.target_summary,
            "config_fingerprint": self.config_fingerprint,
        }


def may_decide(
    pending: PendingApproval,
    identity: str | None,
    *,
    approvers: Collection[str] = (),
    approvers_only: bool = False,
) -> bool:
    """调用者是否有权列出 / 决策这条待审批项。

    - 鉴权未启用：没有身份可比，一律放行（fail-open，见 ``api/approvals.py`` 的说明）；
    - ``approvers_only=True``：只允许 ``approvers`` 里的身份，支持职责分离——审批人是
      **岗位**，可列出并批准**任何人**触发的项，不限于自己发起的；
    - 默认模式（``owner``）：要求 ``pending.user`` 精确匹配，即只有触发人自己。

    两个调用点必须都走这里：``GET /approvals`` 的过滤与 ``POST /approvals/{id}`` 的授权，
    否则会出现「列表里看不见、却批得动」这种最糟的组合。
    """
    if identity is None:
        return True
    if approvers_only:
        return identity in set(approvers)
    return pending.user == identity


@dataclass(frozen=True)
class ApprovalOutcome:
    """一次审批决策的续跑结果。

    status:
    - "completed"        —— 本轮跑完，reply 是 Agent 最终回答；
    - "pending_approval" —— 续跑又触发新的写命令，next_approval_id 是下一个待审批项
                            （同一条线程，多写命令链）。
    """

    approval_id: str
    decision: str
    status: str
    reply: str
    next_approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为 api 层直接可序列化的结构。"""
        return {
            "approval_id": self.approval_id,
            "decision": self.decision,
            "status": self.status,
            "reply": self.reply,
            "next_approval_id": self.next_approval_id,
        }
