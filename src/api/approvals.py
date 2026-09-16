"""人工审批路由：GET /approvals 待审批列表、POST /approvals/{id} 提交决策。

Agent 的写命令（config/agent_actions.yaml）在执行前会中断，等人工在这里决策。
这是**唯一**能让写命令真正执行的入口。

鉴权（config/settings.yaml 的 auth 段）：
启用后本组路由要求 `Authorization: Bearer <密钥>`（见 src/api/deps.py），并且
**按归属隔离**——只看得到、只批得动**自己触发**的待审批项。归属由密钥推出的身份
判定（`interfaces.approval.may_decide`，与会话那边的 `is_owner` 同款语义）。
非本人的项一律 **404**，与「未知 / 已过期 / 已处理」同形——调用者分不出两者，
这正是想要的。

未启用 auth 时是**无鉴权**状态：任何能访问该端口的人都能批准写命令。
缓解措施：审批 id 为随机不可枚举值、server.host 默认仅 127.0.0.1、白名单固定。
**注意：原设计还有一条「出厂 agent_actions.enabled=false」的缓解，但 2026-09-10 起本仓库
已把该能力打开，这条不再成立**——写通道现在是活的。
**绑到 0.0.0.0 之前请务必打开 auth**，否则任何能访问端口的人都可能批准写命令。
详见 README「安全说明」。
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.core.agent import OpsAgent
from src.interfaces.approval import (
    ApprovalError,
    ApprovalUnavailableError,
    PendingApproval,
    may_decide,
)
from src.utils.logger import write_audit
from src.utils.auth import AuthConfigError, parse_approvers
from src.utils.time import now_iso

logger = logging.getLogger("aiops_assistant.api.approvals")

router = APIRouter()


def _agent(request: Request) -> OpsAgent:
    return request.app.state.agent


class ApprovalRequest(BaseModel):
    """/approvals/{id} 请求体。"""

    # 用 Literal 而非 str：edit 之类的值在入参校验阶段就 422，
    # 永远到不了 resume —— 非法负载会**永久毒化** LangGraph 线程（已实测）。
    action: Literal["approve", "reject"] = Field(..., description="审批决策")
    reason: str = Field("", max_length=1000, description="审批理由（仅记日志，不影响命令内容）")


class ApprovalResponse(BaseModel):
    """/approvals/{id} 响应体。"""

    approval_id: str
    decision: str
    status: str = Field(..., description="completed=本轮跑完；pending_approval=又触发下一条写命令")
    reply: str
    next_approval_id: str | None = None


def _not_found(approval_id: str) -> HTTPException:
    """统一的 404 文案。

    **归属不匹配刻意用与「未知 id」完全相同的措辞**——两者对调用者不可区分，
    不泄露「这条审批存在，只是不是你的」。
    """
    return HTTPException(
        status_code=404, detail=f"未知、已过期或已被处理的审批 id: {approval_id}"
    )


def _approval_policy(request: Request) -> tuple[tuple[str, ...], bool]:
    auth = request.app.state.settings.auth
    try:
        approvers = parse_approvers(auth.approver_identities)
    except AuthConfigError as exc:
        raise HTTPException(status_code=500, detail=f"审批人配置无效: {exc}") from exc
    return approvers, auth.approval_mode == "approvers_only"


def _find_owned(
    agent: OpsAgent,
    approval_id: str,
    identity: str | None,
    *,
    approvers: tuple[str, ...] = (),
    approvers_only: bool = False,
) -> PendingApproval | None:
    """在调用者的可见范围里找这条待审批项。

    - 找到且**属于他**（或鉴权关闭）→ 返回它；
    - 找到但**不是他的** → 404（与「未知 id」同形）；
    - **队列里没有** → 返回 `None`。**不在这里报错**：那条路径还要分「真的没有」
      与「能力不可用（→ 503）」两种情况，交给 `decide()` 按它原本的逻辑处理
      （能力不可用时 `pending_approvals()` 恒为空，正好落到这里）。

    返回值同时供审计用——审计要记下这条待审批包含了哪些命令。
    """
    for pending in agent.pending_approvals():
        if pending.approval_id == approval_id:
            if not may_decide(
                pending,
                identity,
                approvers=approvers,
                approvers_only=approvers_only,
            ):
                raise _not_found(approval_id)
            return pending
    return None


def _audit_decision(
    approval_id: str, action: str, reason: str, identity: str | None, pending: PendingApproval
) -> None:
    """把「**谁**、什么时候、对哪条待审批做了什么决策」写进审计日志。

    **写在命令执行之前。** 写在之后的话，进程若死在执行中途就永远丢了这一条——
    而「谁批准了这条命令」恰恰是事后追责时最需要的那一条。命令本身由执行器单独写
    `record_execution`；两条对照就能还原完整经过（谁批的 → 跑没跑）。
    """
    write_audit({
        "action": "approval_decision",
        "approval_id": approval_id,
        "decision": action,
        # 空串 = 当时鉴权未启用（没有身份可言）。这一条本身就是审计要回答的问题。
        "decided_by": identity or "",
        "reason": reason,
        "commands": [a.get("name") for a in pending.actions],
        "decided_at": now_iso(),
    })


@router.get("/approvals", response_model=list[dict[str, Any]])
def list_approvals(
    request: Request,
    user: str | None = Depends(require_auth),
) -> list[dict[str, Any]]:
    """列出当前待人工决策的写命令调用（新的在前）。无写命令能力时返回空列表。

    鉴权开启时**只列调用者自己的**；鉴权关闭时列全部（`user is None`），
    后者是改造前的行为。
    """
    pending = _agent(request).pending_approvals()
    approvers, approvers_only = _approval_policy(request)
    return [
        p.to_dict()
        for p in pending
        if may_decide(
            p,
            user,
            approvers=approvers,
            approvers_only=approvers_only,
        )
    ]


@router.post("/approvals/{approval_id}", response_model=ApprovalResponse)
def decide(
    approval_id: str,
    body: ApprovalRequest,
    request: Request,
    user: str | None = Depends(require_auth),
) -> ApprovalResponse:
    """提交一次审批决策并续跑。

    approve → 命令真正执行；reject → 放弃执行（两者都会让 Agent 继续把话说完）。
    审批 id 单次有效：重复提交返回 404。
    """
    agent = _agent(request)
    logger.info("收到审批决策 id=%s action=%s from=%s", approval_id, body.action, user)
    approvers, approvers_only = _approval_policy(request)
    pending = _find_owned(
        agent,
        approval_id,
        user,
        approvers=approvers,
        approvers_only=approvers_only,
    )
    if pending is not None:
        # 只对**真实存在**的待审批项写审计：队列里没有的 id（多半是打错或探测）
        # 由下面的 decide() 报 404，不该在审计里留下一串「批准」的假记录。
        _audit_decision(approval_id, body.action, body.reason, user, pending)
    try:
        outcome = agent.decide(approval_id, action=body.action, reason=body.reason)
    except ApprovalUnavailableError as exc:
        # 功能没开 ≠ 找错了 id：前者是部署问题，后者是调用问题
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApprovalResponse(**outcome.to_dict())
