"""聊天路由：POST /chat。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from src.core.agent import OpsAgent

logger = logging.getLogger("ops_agent.api.chat")

router = APIRouter()


class ChatRequest(BaseModel):
    """/chat 请求体。"""

    user: str = Field(..., description="调用方标识，如 'ops'")
    message: str = Field(..., min_length=1, description="用户输入的消息")


class ChatResponse(BaseModel):
    """/chat 响应体。"""

    reply: str = Field(..., description="机器人回复")


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request) -> ChatResponse:
    """接收用户消息，经 OpsAgent 返回回复（Agent-first + 关键词兜底）。"""
    agent: OpsAgent = request.app.state.agent
    logger.info("收到消息 from=%s: %s", req.user, req.message)
    reply = agent.chat(req.message)
    logger.info("回复 from=%s: %s", req.user, reply)
    return ChatResponse(reply=reply)
