"""会话接口始终要求有效 Token。会话归创建者所有，还须具有会话主机访问权；
不存在、他人会话及已撤销主机权限统一返回 404。存储不可用时写入/读取返回 503。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from src.api.deps import require_auth
from src.interfaces.approval import ApprovalUnavailableError
from src.interfaces.session import (
    SESSION_NAME_MAX_CHARS,
    SessionError,
    SessionInfo,
    SessionStore,
    is_owner,
    normalize_session_name,
)
from src.api.access import can_access, require_host, visible_host_ids

logger = logging.getLogger("aiops_assistant.api.sessions")

router = APIRouter()


class SessionCreateRequest(BaseModel):
    """POST /sessions 请求体。

    **创建者不在这里**：它由密钥推出（见 `require_auth`）。自报的用户名无从核实，
    让它出现在请求体里只会制造「以为隔离了、其实没有」的错觉。
    """

    name: str | None = Field(
        None,
        description=f"给人识别的标签（任意可见字符，最长 {SESSION_NAME_MAX_CHARS}）；"
                    "不填 = 未命名。**不要求唯一**，同一人可以有多段同名对话",
    )
    host_id: str | None = Field(
        None,
        description="会话固定主机 ID；多主机部署必须显式提供，创建后不可修改",
    )

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        """空串 / 纯空白 → None（视同「没填」）；否则归一化并卡住非法名字。

        与 `ChatRequest` 同款处理：Swagger 会把字段填成示例值，用户「清空输入框」
        是最常见的操作，把 `""` 当成「填了一个空名字」而 422 会很难用。
        """
        if value is None:
            return None
        if not isinstance(value, str):  # 交给 pydantic 原本的类型校验去报错
            return value
        if not value.strip():
            return None
        try:
            return normalize_session_name(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class SessionRenameRequest(BaseModel):
    """PATCH /sessions/{id} 请求体。"""

    name: str = Field(..., description="新标签；空串 / 纯空白 = **清除**标签")

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        """空 → `""`（清除标签）；否则走与建会话**同一套**归一化与校验。

        「清除」是一种对称操作：既然 `POST /sessions` 允许建未命名会话，改名就该允许
        退回未命名——否则名字只有单程票，改错了没法撤销。
        """
        if not isinstance(value, str):
            return value
        if not value.strip():
            return ""
        try:
            return normalize_session_name(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


def _store(request: Request) -> SessionStore:
    """取会话存储；持久化未启用 → 503（「功能没开」，区别于「找错了 id」的 404）。"""
    store = request.app.state.container.sessions()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="持久化未启用或不可用（见 config/settings.yaml 的 storage 段）",
        )
    return store


def _owned_or_404(request: Request, store: SessionStore, session_id: str, user: str | None) -> SessionInfo:
    """取会话并校验归属；**不存在与不属于调用者一律 404**（同形，不泄露 id 是否存在）。

    归属判定用 `interfaces.is_owner`——那是唯一的实现，`/chat` 走的是同一个。
    """
    info = store.get(session_id)
    if info is None or not is_owner(info, user) or not can_access(request, info.host_id):
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    return info


@router.post("/sessions", response_model=dict[str, Any], status_code=201)
def create_session(
    body: SessionCreateRequest,
    request: Request,
    user: str | None = Depends(require_auth),
) -> dict[str, Any]:
    """新建一段会话并返回其 id（之后每轮 `/chat` 都要带上它）。

    这是建会话的**唯一**入口（`/chat` 不再顺手建会话）：一次请求只做一件事，
    调用方也不必再猜「我这次是新建还是续聊」。
    """
    store = _store(request)
    hosts = request.app.state.container.hosts()
    if body.host_id:
        host = require_host(request, body.host_id)
    else:
        enabled = [h for h in hosts.enabled() if can_access(request, h.host_id)]
        if len(enabled) != 1:
            raise HTTPException(
                status_code=422,
                detail="多主机模式创建会话必须明确提供 host_id",
            )
        host = enabled[0]
    try:
        info = store.create(user=user, name=body.name or "", host_id=host.host_id)
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    logger.info("会话已创建 id=%s user=%s name=%r", info.session_id, info.user, info.name)
    return info.to_dict()


@router.get("/sessions", response_model=list[dict[str, Any]])
def list_sessions(
    request: Request,
    limit: int = Query(50, ge=1, le=500, description="最多返回条数"),
    user: str | None = Depends(require_auth),
) -> list[dict[str, Any]]:
    """仅列当前 Token 身份仍可访问的主机会话，过滤在 LIMIT 之前。
    存储关闭返回空列表，存储故障返回 503。
    """
    store = request.app.state.container.sessions()
    if store is None:
        return []
    try:
        return [s.to_dict() for s in store.list(user=user, limit=limit, host_ids=visible_host_ids(request))]
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/sessions/{session_id}", response_model=dict[str, Any])
def get_session(
    session_id: str, request: Request, user: str | None = Depends(require_auth)
) -> dict[str, Any]:
    """读取本人仍有主机访问权的会话，返回元信息及最近 100 条消息。

    包括 pending/abandoned/unknown 等展示状态；未知或越权统一返回 404。
    """
    store = _store(request)
    try:
        info = _owned_or_404(request, store, session_id, user)
        payload = info.to_dict()
        payload["messages"] = [m.to_dict() for m in store.messages(session_id)]
        return payload
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.patch("/sessions/{session_id}", response_model=dict[str, Any])
def rename_session(
    session_id: str,
    body: SessionRenameRequest,
    request: Request,
    user: str | None = Depends(require_auth),
) -> dict[str, Any]:
    """改会话标签（`name` 为空串则清除标签）。

    会话名只是一层供人识别的薄壳：**不参与选中会话**（选中只认 `session_id`），
    所以改名不影响任何进行中的对话。本操作**不更新 `updated_at`**——改名不是
    「会话活动」，刷新它会让 `GET /sessions` 的排序与 TTL 过期时机跟着跳动。
    """
    store = _store(request)
    try:
        _owned_or_404(request, store, session_id, user)
        if not store.rename(session_id, name=body.name, user=user):
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        # 更新和读取不在同一事务；并发删除或 TTL 清理仍可能移除会话。
        info = _owned_or_404(request, store, session_id, user)
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    logger.info("会话已改名 id=%s name=%r", session_id, body.name)
    return info.to_dict()


@router.delete("/sessions/{session_id}", response_model=dict[str, Any])
def delete_session(
    session_id: str, request: Request, user: str | None = Depends(require_auth)
) -> dict[str, Any]:
    """删除会话连同其全部消息，并**作废该会话的未决审批**。

    未决审批必须先作废再删会话：删完就拿不到 approval_id 了，而一个仍然挂着的
    写命令中断是可以被批准执行的——那等于删了会话却留着一条能改系统的句柄。
    宁可让人重新发起，也不能保留可执行的陈旧审批。

    **归属检查必须排在作废审批之前**：`cancel_pending` 不可逆（把那一轮永久标成
    abandoned），先作废再发现「不是你的会话」等于白废掉别人一条待执行的写命令。
    `store.delete` 内部还有一道同事务守卫，防的是检查与删除之间的竞态。

    已删除 / 从未存在 / 不属于调用者 一律 404（**不静默重建**，也不区分后两者）。
    """
    store = _store(request)
    agent = request.app.state.agent
    try:
        _owned_or_404(request, store, session_id, user)
        pending_ids = [
            p.approval_id for p in agent.pending_approvals(strict=True)
            if p.session_id == session_id
        ]
    except (SessionError, ApprovalUnavailableError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    cancelled = [aid for aid in pending_ids if agent.cancel_pending(aid)]
    if len(cancelled) != len(pending_ids):
        raise HTTPException(status_code=409, detail="Pending approval is being processed; retry session deletion")

    try:
        deleted = store.delete(session_id, user=user)
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    logger.info("会话已删除 id=%s user=%s（作废未决审批 %d 条）", session_id, user, len(cancelled))
    return {"session_id": session_id, "deleted": True, "cancelled_approvals": len(cancelled)}
