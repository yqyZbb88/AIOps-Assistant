"""聊天路由：POST /chat。

请求体新增**可选** `session_id`、响应体恒回带 `session_id`（M4 第二批）。
这是对外 HTTP 契约唯一的扩充点，且**只增不改**：不传 session_id 时行为与改造前
完全一致（每次调用独立），既有调用方无需改动。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.core.agent import OpsAgent
from src.interfaces.session import (
    SESSION_NAME_MAX_CHARS,
    SessionNotFoundError,
    normalize_session_name,
)

logger = logging.getLogger("ops_agent.api.chat")

router = APIRouter()


class ChatRequest(BaseModel):
    """/chat 请求体。

    会话规则只有一条：**有 `session_id` 就用它续聊，没有就新建一段对话。**

    - `session_id`：服务端生成的随机 id，**选中会话的唯一方式**。须已存在，未知则 404。
    - `session_name`：给新会话起个**给人识别的名字**（标签）。它**不参与选中会话**——
      传名字永远不会接到旧对话上；想让某段对话接得上，把它的 `session_id` 带回来。
      同时给了 `session_id` 和名字时，**名字被忽略**。

    名字**不要求唯一**，同一个人可以有两段都叫「生产排查」的对话——这也是为什么
    `GET /sessions` 里除了名字还要看 id/时间/标题来区分。

    注意：**不要把 `user` 当会话键**——同一人开两个话题会互相污染，多用户共用一个
    标识（本服务无鉴权，前端常这么写）更会让所有人串进同一条对话。

    **空串与纯空白视同「未提供」**（两个会话字段都是）：它们本就是可选字段，
    而「我把输入框清空了」的意图就是「我没填」。把 `""` 当成「填了一个空名字」
    而拒绝，会让 Swagger UI 里清空输入框这种最常见的操作直接 422。
    """

    user: str = Field(..., description="调用方标识，如 'ops'")
    message: str = Field(..., min_length=1, description="用户输入的消息")
    session_id: str | None = Field(
        None, description="服务端生成的会话 id（须已存在，未知则 404）；空串视同未提供"
    )
    session_name: str | None = Field(
        None,
        description=f"给**新会话**起的标签（任意可见字符，最长 {SESSION_NAME_MAX_CHARS}）；"
                    "不参与选中会话，传 id 时被忽略；空串视同未提供",
    )

    @field_validator("session_id")
    @classmethod
    def _blank_id_is_absent(cls, value: str | None) -> str | None:
        """空串 / 纯空白 → None（视同没给 id）。

        不加这个的话，`session_id: ""` 会一路走到「按 id 续聊」分支并被判为假值，
        落到「都没给 → 新建会话」——**结果虽然一样，但它走的是一条说得通的路径，
        而不是「把一个空串当成了某个 id」**。顺手 `strip()`：id 是服务端生成的十六进制，
        绝不含空格，所以去掉首尾空白只会救回「复制粘贴多带了空格」这种情况。
        """
        if value is None:
            return None
        if not isinstance(value, str):  # 交给 pydantic 原本的类型校验去报错
            return value
        return value.strip() or None

    @field_validator("session_name")
    @classmethod
    def _normalize_name(cls, value: str | None) -> str | None:
        """空串 → None；否则在**入参校验阶段**归一化并卡住非法名字。

        放这里的理由与 `approval.action` 用 `Literal` 一样：非法输入应当直接 422，
        不要让它一路走到存储层再变成一个 500。

        **注意这里会改写值**：全角字母、各种空格变体都会被归一化，
        所以响应里回带的 `session_name` 可能与请求里的不完全一样（这是有意的——
        见 `interfaces/session.py::normalize_session_name` 的说明）。
        """
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        # 纯空白先在这里拦掉：否则 normalize_session_name 会以「名字不能为空」拒绝它，
        # 而用户的意图明明只是「没填」
        if not value.strip():
            return None
        try:
            return normalize_session_name(value)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

class ChatResponse(BaseModel):
    """/chat 响应体。"""

    reply: str = Field(..., description="机器人回复")
    session_id: str | None = Field(
        None, description="本次使用的会话 id（持久化未启用时为 null）"
    )
    session_name: str | None = Field(
        None, description="本次使用的会话名（未命名会话为 null）"
    )


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request) -> ChatResponse:
    """接收用户消息，经 OpsAgent 返回回复（Agent-first + 关键词兜底）。

    持久化启用时，同一 `session_id` 或 `session_name` 的多轮会带上历史。
    未知 `session_id` → 404；`session_name` 不存在则按此名新建。
    """
    agent: OpsAgent = request.app.state.agent
    logger.info(
        "收到消息 from=%s session_id=%s session_name=%s: %s",
        req.user, req.session_id, req.session_name, req.message,
    )
    try:
        turn = agent.chat_turn(
            req.message,
            session_id=req.session_id,
            session_name=req.session_name,
            user=req.user,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "回复 from=%s session=%s(%s): %s",
        req.user, turn.session_id, turn.session_name, turn.reply,
    )
    return ChatResponse(
        reply=turn.reply, session_id=turn.session_id, session_name=turn.session_name
    )
