"""对话会话契约：中性类型 + 存储抽象 + Agent 多轮能力探测（无第三方依赖）。

背景：Agent 原先**结构上只能单轮**——每轮一个临时 thread_id、跑完即销毁，
`/chat` 连会话概念都没有。本模块描述「一段对话」与「一轮的结果」的形状，
让 core / api 能在**不感知 langchain** 的前提下做多轮。

两条路线里本项目选的是**路线 B**：历史表是唯一真相，每轮从表里取窗口内的历史
重新拼给模型；checkpointer 继续只当「审批中断的临时草稿纸」（每轮新 thread_id、
跑完即删，用法与改造前完全一致）。

分层：
- `ChatMessage` / `SessionInfo` / `TurnResult` 是**中性数据**，谁都能用；
- `SessionStore` 是抽象基类，实现落在 src/storage/session.py（sqlite）；
- `HistoryCapable` 是**能力探测** Protocol，实现在 src/agent/assistant.py。

为什么 `HistoryCapable` 用 Protocol 而非并入 `Assistant`：见 interfaces/assistant.py
里 `ApprovalCapable` 的同类说明——把方法加进 `Assistant` 会让所有既有假对象结构性失效。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Collection, Protocol, Sequence, runtime_checkable

# 消息角色：只存 user / assistant 两种。
# **不存 tool 调用与工具返回**——中间那几轮 tool-call 消息既难序列化也没必要
# （下轮模型需要数据会重新调工具），存了反而要维护一整套 BaseMessage 序列化。
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# 消息状态：
# - final            正常完成的一轮（模型窗口会看到）
# - pending_approval 该轮触发了写命令审批、最终回答尚未产生（窗口**看不到**，
#                    否则模型会以为「还在等批准」而重复催促；见 status 说明）
# - abandoned        审批已作废（进程重启 / 过期 / 会话删除），永远不会再有下文
STATUS_FINAL = "final"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_ABANDONED = "abandoned"

# 每轮默认重发给模型的历史消息条数上限（限 token；实现与调用方共用同一个默认值）
DEFAULT_MESSAGE_WINDOW = 20

# 可以进入模型上下文的窗口状态：**只有** final。
# pending/abandoned 是控制面信息，展示给用户看（GET /sessions/{id}），但不给模型——
# 否则模型每轮都读到一段「等我批准」，会诱发「还在等批准」的幻觉。
WINDOW_STATUSES: tuple[str, ...] = (STATUS_FINAL,)


class SessionError(Exception):
    """会话层错误（存储不可用、会话不存在等）。"""


class SessionNotFoundError(SessionError):
    """会话不存在 / 已被删除 / 已过期。

    与「存储不可用」区分开：api 层据此返回 404 而非 503。
    **不静默重建**——否则 DELETE 形同虚设（删掉的会话一被引用就复活）。
    """


@dataclass(frozen=True)
class ChatMessage:
    """一条对话消息（中性类型，与 langchain 的 BaseMessage 无关）。"""

    role: str
    content: str
    seq: int = 0
    status: str = STATUS_FINAL
    approval_id: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为 api 层直接可序列化的结构。"""
        return {
            "role": self.role,
            "content": self.content,
            "seq": self.seq,
            "status": self.status,
            "approval_id": self.approval_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class SessionInfo:
    """一段对话的元信息。"""

    session_id: str
    user: str = ""
    title: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    pending_approvals: int = 0
    message_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转为 api 层直接可序列化的结构。"""
        return {
            "session_id": self.session_id,
            "user": self.user,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pending_approvals": self.pending_approvals,
            "message_count": self.message_count,
        }


@dataclass(frozen=True)
class TurnResult:
    """一轮对话的结果（Agent 侧返回给 core 的形状）。

    status:
    - "ok"               —— 本轮跑完，text 是最终回答；
    - "pending_approval" —— 触发了写命令审批，text 是待审批提示，approval_id 有值。

    **为什么不能只回一个 str**：core 需要拿到 approval_id 才能把那一轮记成
    `pending_approval` 并在审批后回填；只回文本的话，pending 行根本无从落。
    """

    text: str
    status: str = "ok"
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "status": self.status, "approval_id": self.approval_id}


@dataclass(frozen=True)
class ChatReply:
    """一次 `/chat` 的结果：回答文本 + 会话 id。

    会话 id **恒回带**（新建时是新生成的，续聊时是原来的）：只在新建时回带的话，
    客户端与测试都得处理「有时有、有时没有」，是最容易漏传分支的形态。
    """

    reply: str
    session_id: str | None = None
    #: 本轮若触发写命令审批，这里是被中断那一轮的审批 id（`/approvals/{id}` 用）
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "session_id": self.session_id,
            "approval_id": self.approval_id,
        }


class SessionStore(ABC):
    """会话与消息的存储抽象（实现见 src/storage/session.py）。

    实现约定：
    - **写失败必须抛异常**，不许静默吞——调用方（core/chat.py）需要据此把
      持久化标记为 degraded；「历史表是唯一真相」与「写失败就当没发生」是矛盾的。
    - 读失败可以降级为空结果，但调用方要 `logger.warning(exc_info=True)`。
    - `healthy` / `last_error` 供 /health 暴露降级状态，默认恒为正常。
    """

    @property
    def healthy(self) -> bool:
        """持久化是否正常（写失败后置 False）。"""
        return True

    @property
    def last_error(self) -> str | None:
        """最近一次失败的简述（供 /health 展示）。"""
        return None

    # -- 会话生命周期 -------------------------------------------------------
    @abstractmethod
    def create(self, *, user: str = "", title: str = "") -> SessionInfo:
        """新建一个会话并返回其元信息。不传 session_id，由存储层生成。"""

    @abstractmethod
    def get(self, session_id: str) -> SessionInfo | None:
        """取会话元信息；不存在返回 None。"""

    @abstractmethod
    def list(self, *, limit: int = 50) -> list[SessionInfo]:
        """列出会话，最近更新的在前。"""

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        """删除会话**连同其全部消息**（同事务；不依赖外键）。

        返回是否真的删掉了。调用方另需作废该会话的未决审批（见 ApprovalCancellable）。
        """

    # -- 消息 ---------------------------------------------------------------
    @abstractmethod
    def history(self, session_id: str, *, limit: int = 20) -> list[ChatMessage]:
        """取**可进入模型上下文**的最近 limit 条消息，按时间正序（旧→新）。

        只返回 `final` 状态的消息（见 WINDOW_STATUSES）。
        """

    @abstractmethod
    def messages(self, session_id: str, *, limit: int = 100) -> list[ChatMessage]:
        """取**全部状态**的消息供展示（含 pending/abandoned），按时间正序。

        与 history() 的区别正是「给用户看」与「给模型看」的区别。
        """

    @abstractmethod
    def append_turn(
        self,
        session_id: str,
        *,
        user_content: str,
        assistant_content: str,
        status: str = STATUS_FINAL,
        approval_id: str | None = None,
    ) -> None:
        """把一轮的 user + assistant 两条消息**在同一事务内**写入。

        同事务是硬要求：若只写进 user 行、assistant 行失败，下一轮的窗口里
        就会出现一条没有回答的 user 消息，模型会把旧问题当作新问题再答一遍。
        """

    @abstractmethod
    def update_pending(
        self,
        approval_id: str,
        *,
        content: str,
        status: str = STATUS_FINAL,
        next_approval_id: str | None = None,
    ) -> bool:
        """按 **approval_id** 定位某轮 pending 并推进到下一步。返回是否命中。

        - status=final            → **回填最终回答**（content），approval_id 保留为历史痕迹
        - status=pending_approval → **链式审批**：content 换成新的待审批提示，
                                    approval_id 换成 next_approval_id
        - status=abandoned        → 作废（审批已不可能再有下文）

        **不需要 session_id**：approval_id 由 uuid4 生成、全局唯一，单独就能定位到行——
        这省掉了「把 session_id 一路穿到审批记录里再带回来」的整条管线。
        调用方（core/chat.py）在 decide() 后只有 approval_id，正好够用。

        用 UPDATE 原地改而不是追加新行：追加会让窗口里夹着「等我批准」的控制面噪音，
        且 seq 语义变乱。
        """

    @abstractmethod
    def reconcile_pending(self, live_approval_ids: Collection[str]) -> int:
        """把**不在** live_approval_ids 里的 pending **轮**标为 abandoned，返回轮数。

        返回轮数而非行数：一轮有 user + assistant 两行，数行会翻倍（与 `pending_approvals`
        的口径一致）。

        - 开库时传空集合：新进程里任何 pending 行必然是孤儿（审批存在进程内存里，
          不可能跨重启存活）；
        - 运行期传入 `approvals.list()` 的 id 集合：覆盖 TTL/容量淘汰造成的孤儿。
        """

    @abstractmethod
    def purge_expired(self) -> int:
        """清理过期会话（连同消息），返回删除的会话数。由调用方节流后低频触发。"""


@runtime_checkable
class HistoryCapable(Protocol):
    """支持多轮历史的 Agent（可选能力）。

    **方法名必须是新的（reply_turn），不能是给 reply 加一个带默认值的 kwarg。**
    原因：`runtime_checkable` 只检验**方法名是否存在**，不检验签名——给 reply 加
    `history=None` 的话，既有的 FakeAssistant（只实现 `reply(self, message)`）
    依然会被 `isinstance(assistant, HistoryCapable)` 判为 True，然后在真正调用时
    TypeError。守卫形同虚设。换新方法名才有意义，这也正是 ApprovalCapable 能成立的原因。

    另一个坑：Protocol 里**只能有方法成员**。一旦声明数据成员，`isinstance` 会直接
    抛 TypeError（而不是返回 False），测试里很容易踩。

    调用方（core/chat.py）用 isinstance 守卫，不支持的实现退回单轮 `reply(message)`。
    """

    def reply_turn(
        self, message: str, *, history: Sequence[ChatMessage] = ()
    ) -> TurnResult:
        """带着历史跑一轮，返回最终回答或待审批提示；失败时抛异常由调用方回落。"""
