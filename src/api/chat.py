"""聊天路由：POST /chat。

**契约变更（2026-09-12，破坏性）**：请求体删掉了 `user` 与 `session_name`，
`session_id` 由可选改为**必填**。理由有两条：

1. 调用者身份改由**密钥**推出（见 `src/api/deps.py`），请求体里自报的用户名不再采信——
   自报的东西无从核实，基于它的任何校验都只是装饰；
2. **建会话**拆到了 `POST /sessions`（名字在建会话时给）。`/chat` 只管聊天，
   不再兼职建会话。

响应体三个键不变（`reply` / `session_id` / `session_name`），`session_name` 现在
是**回带**该会话的标签。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.api.deps import require_auth
from src.core.agent import OpsAgent
from src.interfaces.session import SessionNotFoundError

logger = logging.getLogger("ops_agent.api.chat")

router = APIRouter()


class ChatRequest(BaseModel):
    """/chat 请求体。

    **两段式：先 `POST /sessions` 建会话拿 id，之后每轮都带上它。**

    - `message`：本轮用户输入。
    - `session_id`：**必填**。须已存在且属于调用者；未知 / 不属于你一律 404
      （两者同形，不泄露「这个 id 存在，只是不是你的」）。

    **调用者身份不在这里**，它由 `Authorization: Bearer <密钥>` 推出
    （见 config/settings.yaml 的 auth 段）。此前那个 `user` 字段已移除——
    自报的用户名无从核实，基于它的校验只是装饰。客户端若仍带着 `user` 或
    `session_name`，pydantic 会**静默忽略**（默认 `extra="ignore"`）：它们不再是
    可信输入，为它们报 422 只会让既有调用方困惑。
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
        None, description="本次使用的会话 id（持久化未启用时为 null）"
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
    """接收用户消息，经 OpsAgent 返回回复（Agent-first + 关键词兜底）。

    `user` 由 `require_auth` 从密钥推出；鉴权关闭时为 `None`（此时不校验会话归属，
    行为与改造前一致）。持久化启用时，同一 `session_id` 的多轮会带上历史；
    未知 / 不属于调用者的 `session_id` → 404。
    """
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
        turn = agent.chat_turn(req.message, session_id=req.session_id, user=user)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "回复 from=%s session=%s(%s): %s",
        user, turn.session_id, turn.session_name, turn.reply,
    )
    return ChatResponse(
        reply=turn.reply, session_id=turn.session_id, session_name=turn.session_name
    )
