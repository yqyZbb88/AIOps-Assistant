"""聊天路由：POST /chat。

请求体新增**可选** `session_id`、响应体恒回带 `session_id`（M4 第二批）。
这是对外 HTTP 契约唯一的扩充点，且**只增不改**：不传 session_id 时行为与改造前
完全一致（每次调用独立），既有调用方无需改动。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.core.agent import OpsAgent
from src.interfaces.session import SessionNotFoundError

logger = logging.getLogger("ops_agent.api.chat")

router = APIRouter()


class ChatRequest(BaseModel):
    """/chat 请求体。"""

    user: str = Field(..., description="调用方标识，如 'ops'")
    message: str = Field(..., min_length=1, description="用户输入的消息")
    # 可选的会话 id：**不要拿 user 当会话键**——同一人开两个话题会互相污染，多用户
    # 共用一个标识（本服务无鉴权，前端常这么写）更会让所有人串进同一条对话。
    # 不传则新建一个会话，并在响应里回带其 id。
    session_id: str | None = Field(
        None, description="会话 id；不传则新建会话（响应恒回带 session_id）"
    )


class ChatResponse(BaseModel):
    """/chat 响应体。"""

    reply: str = Field(..., description="机器人回复")
    session_id: str | None = Field(
        None, description="本次使用的会话 id（持久化未启用时为 null）"
    )


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request) -> ChatResponse:
    """接收用户消息，经 OpsAgent 返回回复（Agent-first + 关键词兜底）。

    持久化启用时同一 session_id 的多轮会带上历史（Agent 接得上上句）。
    未知 session_id → 404（不静默重建，否则 DELETE 形同虚设）。
    """
    agent: OpsAgent = request.app.state.agent
    logger.info("收到消息 from=%s session=%s: %s", req.user, req.session_id, req.message)
    try:
        turn = agent.chat_turn(req.message, session_id=req.session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("回复 from=%s session=%s: %s", req.user, turn.session_id, turn.reply)
    return ChatResponse(reply=turn.reply, session_id=turn.session_id)
