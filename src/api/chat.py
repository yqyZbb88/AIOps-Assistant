"""聊天始终验证 Token、会话归属与固定主机访问权。
会话存储不可用时返回 503，禁止回退到无主机范围的聊天。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.api.deps import require_auth
from src.api.sessions import _store, _owned_or_404
from src.api.access import require_host
from src.core.agent import OpsAgent
from src.interfaces.session import SessionError, SessionNotFoundError

logger = logging.getLogger("aiops_assistant.api.chat")

router = APIRouter()


class ChatRequest(BaseModel):
    """/chat 请求体。

    message 与既有 session_id 必填，先通过 POST /sessions 创建会话。
    身份始终来自 Bearer Token；额外 user/session_name 字段被忽略。未知或越权
    会话统一为 404，存储不可用为 503，不提供关闭鉴权的分支。
    """

    message: str = Field(..., min_length=1, description="用户输入的消息")
    session_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="会话 id，来自 POST /sessions（须已存在且属于你）",
    )

    @field_validator("session_id")
    @classmethod
    def _blank_id_is_rejected(cls, value: str) -> str:
        """空白直接 422——**绝不能返回 `None`**。

        字段是必填 `str`，而 pydantic v2 **不会重新校验 after-validator 的返回值**：
        返回 `None` 就真的得到 `None`，接着 `if session_id:` 为假、落到「没给 id」的
        分支，白开一段会话（还顺带把 `DELETE` 的语义架空）。宁可报错。
        """
        stripped = value.strip()
        if not stripped:
            raise ValueError("session_id 不能为空或纯空白")
        return stripped


class ChatResponse(BaseModel):
    """/chat 响应体。"""

    reply: str = Field(..., description="机器人回复")
    session_id: str | None = Field(
        None, description="本次使用的既有会话 id；存储不可用时接口返回 503"
    )
    session_name: str | None = Field(
        None, description="本次使用的会话的标签（未命名会话为 null，只读不改）"
    )


@router.post("/chat", response_model=ChatResponse)
def chat(
    req: ChatRequest,
    request: Request,
    user: str | None = Depends(require_auth),
) -> ChatResponse:
    """验证 Token、会话所有者及固定主机范围后调用 Agent；存储不可用返回 503。"""
    agent: OpsAgent = request.app.state.agent
    max_chars = int(request.app.state.settings.chat.max_message_chars)
    if len(req.message) > max_chars:
        raise HTTPException(
            status_code=413,
            detail=f"message 超过上限 {max_chars} 字符",
        )
    logger.info(
        "收到消息 from=%s session_id=%s: %s", user, req.session_id, req.message
    )
    try:
        info = _owned_or_404(request, _store(request), req.session_id, user)
        require_host(request, info.host_id)
        turn = agent.chat_turn(req.message, session_id=req.session_id, user=user)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SessionError as exc:
        raise HTTPException(status_code=503, detail="待审批请求未能保存，请检查会话存储状态后重新发起") from exc
    logger.info(
        "回复 from=%s session=%s(%s): %s",
        user, turn.session_id, turn.session_name, turn.reply,
    )
    return ChatResponse(
        reply=turn.reply, session_id=turn.session_id, session_name=turn.session_name
    )
