"""会话路由：GET /sessions 列表、GET /sessions/{id} 详情、DELETE /sessions/{id} 删除。

对话历史来自 `storage`（config/settings.yaml）控制的 SQLite；持久化未启用时
本组路由整体不可用（详见各端点的降级说明）。

安全提示（残余风险，部署前须知晓）：
本服务当前没有任何鉴权，而 /sessions/* **会把对话原文整段吐出来**——其中可能含
主机名、报错原文、运维细节与写命令的参数。`server.host` 默认仅 127.0.0.1 是当前
唯一的缓解措施。**若要把服务绑到 0.0.0.0，请先自行加一层鉴权（如共享密钥头）。**
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from src.interfaces.session import SessionError, SessionStore

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
    """删除会话连同其全部消息。已删除 / 从未存在的 id 一律 404（**不静默重建**）。"""
    store = _store(request)
    try:
        deleted = store.delete(session_id)
    except SessionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail=f"会话不存在: {session_id}")
    logger.info("会话已删除 id=%s", session_id)
    return {"session_id": session_id, "deleted": True}
