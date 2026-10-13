"""人工审批仍按 owner / approvers_only 判断决策角色。
HTTP 调用始终要求有效 Token，并检查决策人与申请人均能访问目标主机；
主机访问权不会授予审批角色。匿名旧审批不能在页面批准。"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.api.deps import require_auth
from src.api.access import can_access, visible_host_ids
from src.utils.host_access import is_admin, may_access_host
from src.core.agent import OpsAgent
from src.interfaces.approval import (
    ApprovalError,
    ApprovalUnavailableError,
    ApprovalOutcomeUnknownError,
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
    """按审批模式查找待审批项；查找失败不能静默绕过角色检查。
    未找到时由 decide 按现有能力错误契约处理。"""
    try:
        pending_items = agent.pending_approvals(strict=True)
    except ApprovalUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    for pending in pending_items:
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

    只在审批项被成功消费后写入，避免并发请求留下虚假的批准日志。
    执行前的持久化决策由 Agent 的 approval_decisions 表负责。
    """
    write_audit({
        "action": "approval_decision",
        "approval_id": approval_id,
        "decision": action,
        # 空串 = 当时鉴权未启用（没有身份可言）。这一条本身就是审计要回答的问题。
        "decided_by": identity or "",
        "reason": reason,
        "commands": [a.get("name") for a in pending.actions],
        "host_id": pending.host_id,
        "target_summary": pending.target_summary,
        "config_fingerprint": pending.config_fingerprint,
        "decided_at": now_iso(),
    })


@router.get("/approvals", response_model=list[dict[str, Any]])
def list_approvals(
    request: Request,
    user: str | None = Depends(require_auth),
) -> list[dict[str, Any]]:
    """仅列角色允许、且申请人与决策人都具备主机访问权的未决审批。"""
    pending = _agent(request).pending_approvals()
    approvers, approvers_only = _approval_policy(request)
    return [
        p.to_dict()
        for p in pending
        if can_access(request, p.host_id)
        and may_access_host(request.app.state.settings, p.user, p.host_id)
        and may_decide(
            p,
            user,
            approvers=approvers,
            approvers_only=approvers_only,
        )
    ]


@router.get("/approvals/history")
def approval_history(request: Request, user: str = Depends(require_auth),
                     host_id: str | None = None,
                     status: Literal["approve", "reject", "expired", "cancelled", "evicted", "abandoned", "unknown"] | None = None,
                     limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
    """先按当前主机权限与审批模式过滤再分页，管理员不自动获得审批权限。"""
    approvers, approvers_only = _approval_policy(request)
    if approvers_only and user not in approvers:
        return {"storage": "ok", "items": [], "total": 0, "limit": limit, "offset": offset}
    store = request.app.state.container.approval_history()
    if store is None:
        return {"storage": "off", "items": [], "total": 0, "limit": limit, "offset": offset}
    try:
        # 读取已有 Agent 的内存队列只为收敛过期状态，不构造模型或 SSH 连接。
        _agent(request).pending_approvals()
        hosts = visible_host_ids(request)
        if is_admin(request.app.state.settings, user):
            hosts |= store.host_ids()
        if host_id:
            if host_id not in hosts:
                raise HTTPException(status_code=404, detail="主机不存在或不可访问")
            hosts = {host_id}
        return {"storage": "ok", **store.page(identity=user, hosts=hosts,
            approver=approvers_only and user in approvers, status=status, limit=limit, offset=offset)}
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="审批历史暂不可用，请稍后重试") from None


@router.get("/approvals/{approval_id}/progress")
def approval_progress(approval_id: str, request: Request, user: str = Depends(require_auth)):
    """审批 POST 续跑期间的只读进度；沿用审批角色、申请人及主机权限边界。"""
    from fastapi.responses import JSONResponse
    from src.utils.security import redact_sensitive
    store = request.app.state.container.approval_history()
    if store is None:
        raise HTTPException(status_code=503, detail="审批进度存储不可用，不能确认决策或执行结果")
    try:
        result = store.progress(approval_id)
    except Exception:
        raise HTTPException(status_code=503, detail="审批进度暂不可用，请稍后核对历史记录") from None
    approvers, approvers_only = _approval_policy(request)
    if result is None or not can_access(request, result["host_id"]) or not may_access_host(
        request.app.state.settings, result["requester"], result["host_id"]
    ) or not may_decide(PendingApproval(approval_id=approval_id, created_at="", user=result["requester"]),
                       user, approvers=approvers, approvers_only=approvers_only):
        raise _not_found(approval_id)
    return JSONResponse(redact_sensitive(result), headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


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
    if pending is not None and (
        not can_access(request, pending.host_id)
        or not may_access_host(request.app.state.settings, pending.user, pending.host_id)
    ):
        raise _not_found(approval_id)
    try:
        outcome = agent.decide(
            approval_id,
            action=body.action,
            reason=body.reason,
            decided_by=user,
        )
    except ApprovalOutcomeUnknownError as exc:
        if pending is not None:
            _audit_decision(approval_id, body.action, body.reason, user, pending)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ApprovalUnavailableError as exc:
        # 功能没开 ≠ 找错了 id：前者是部署问题，后者是调用问题
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if pending is not None:
        _audit_decision(approval_id, body.action, body.reason, user, pending)
    return ApprovalResponse(**outcome.to_dict())
