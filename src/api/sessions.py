"""会话路由：GET /sessions 列表、GET /sessions/{id} 详情、DELETE /sessions/{id} 删除。

对话历史来自 `storage`（config/settings.yaml）控制的 SQLite；持久化未启用时
本组路由整体不可用（详见各端点的降级说明）。

鉴权（config/settings.yaml 的 auth 段）：
启用后本组路由要求 `Authorization: Bearer <密钥>`（身份由密钥推断，见 src/api/deps.py）。

未启用时仍是**无鉴权**状态：/sessions/* 会把对话原文整段吐给任何能访问该端口的人
——其中可能含主机名、报错原文、运维细节与写命令的参数。`server.host` 默认仅
127.0.0.1 是目前唯一的缓解措施。**绑到 0.0.0.0 之前请务必打开 auth。**
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from src.interfaces.session import STATUS_PENDING_APPROVAL, SessionError, SessionStore

logger = logging.getLogger("ops_agent.api.sessions")

router = APIRouter()


def _store(request: Request) -> SessionStore:
    """取会话存储；持久化未启用 → 503（「功能没开」，区别于「找错了 id」的 404）。"""
    store = request.app.state.container.sessions()
    if store is None:
        raise HTTPException(
            status_code=503,
            detail="持久化未启用或不可用（见 config/settings.yaml 的 storage 段）",
        )
    return store


@router.get("/sessions", response_model=list[dict[str, Any]])
def list_sessions(
    request: Request, limit: int = Query(50, ge=1, le=500, description="最多返回条数")
) -> list[dict[str, Any]]:
    """列出会话，最近更新的在前。

    持久化未启用时返回**空列表**（沿用 GET /approvals 的约定：能力缺失 = 空而非报错）；
    存储故障则返回 503——那是故障，不该伪装成「没有会话」。
    """
    store = request.app.state.container.sessions()
    if store is None:
        return []
    try:
        return [s.to_dict() for s in store.list(limit=limit)]
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/sessions/{session_id}", response_model=dict[str, Any])
def get_session(session_id: str, request: Request) -> dict[str, Any]:
    """取单个会话：元信息 + 全部消息。

    消息含 **全部状态**（final / pending_approval / abandoned）——这是给**人**看的口，
    与给模型看的窗口不同（窗口只取 final，见 SessionStore.history 的说明）。
    """
    store = _store(request)
    try:
        info = store.get(session_id)
        if info is None:
            raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
        payload = info.to_dict()
        payload["messages"] = [m.to_dict() for m in store.messages(session_id)]
        return payload
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.delete("/sessions/{session_id}", response_model=dict[str, Any])
def delete_session(session_id: str, request: Request) -> dict[str, Any]:
    """删除会话连同其全部消息，并**作废该会话的未决审批**。

    未决审批必须先作废再删会话：删完就拿不到 approval_id 了，而一个仍然挂着的
    写命令中断是可以被批准执行的——那等于删了会话却留着一条能改系统的句柄。
    继承 M3 的 fail-safe 取向：宁可让人重新发起。

    已删除 / 从未存在的 id 一律 404（**不静默重建**）。
    """
    store = _store(request)
    agent = request.app.state.agent
    try:
        pending_ids = [
            m.approval_id
            for m in store.messages(session_id)
            if m.status == STATUS_PENDING_APPROVAL and m.approval_id
        ]
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    cancelled = [aid for aid in pending_ids if agent.cancel_pending(aid)]

    try:
        deleted = store.delete(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    logger.info("会话已删除 id=%s（作废未决审批 %d 条）", session_id, len(cancelled))
    return {"session_id": session_id, "deleted": True, "cancelled_approvals": len(cancelled)}
