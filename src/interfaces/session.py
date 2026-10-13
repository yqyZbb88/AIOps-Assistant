"""对话会话契约：中性类型、存储抽象和多轮能力探测。

定义聊天消息、会话信息、单轮结果和会话存储接口，使 core/api 能在不依赖 LangChain
的前提下支持多轮对话。历史记录以会话表为唯一事实来源，每轮只读取窗口内历史传给模型；
checkpointer 仅保存审批中断所需的临时状态。
"""
from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Collection, Protocol, Sequence, runtime_checkable

# 持久历史仅存用户与最终回答，工具中间态留在当轮，避免跨版本消息序列化耦合。
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

# 消息状态：
# - final            正常完成的一轮（模型窗口会看到）
# - pending_approval 该轮触发了写命令审批、最终回答尚未产生（窗口**看不到**，
#                    否则模型会以为「还在等批准」而重复催促；见 status 说明）
# - abandoned        审批已作废（进程重启 / 过期 / 会话删除），永远不会再有下文
STATUS_FINAL = "final"
# 决策已提交但结果无法确认；与当前申请作废分开，链式前序动作可能已完成。
STATUS_UNKNOWN = "unknown"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_ABANDONED = "abandoned"

# 模型历史按消息条数限制，不是 token 预算。
DEFAULT_MESSAGE_WINDOW = 20

#: 会话名（客户端自定义别名）的格式。
#: **限制长度是硬要求**：限制外部输入造成的存储和展示开销；长度按 Unicode 码点计算。
#: 名字**不等于** session_id——id 仍是服务端生成的 32 位随机值。
SESSION_NAME_MAX_CHARS = 64

#: 会话名**拒绝**的 Unicode 类别（而不是「只允许某几种文字」）：
#:   Cc 控制字符（NUL、ESC、换行…）——会污染日志、能注入终端转义
#:   Cf 格式字符（零宽空格 U+200B、BOM U+FEFF、RTL 覆盖 U+202E、软连字符…）——**肉眼看不见**
#: 其余**一律放行**：假名、重音字母、中文标点、emoji、阿拉伯文……都行。
#:
# 按类别拒绝隐形控制符，保留其他语言的可见文字；这不是任意显示风险的完整过滤。
_SESSION_NAME_BANNED_CATEGORIES = frozenset({"Cc", "Cf"})

#: 连续空白（含换行/制表/不换行空格/全角空格）→ 单个普通空格
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_session_name(name: str) -> str:
    """返回经 NFKC 规范化、折叠空白后的标签；非法值抛 ValueError。

    规范化后拒绝 Cc/Cf，限制为 1～64 个 Unicode 码点。调用方必须使用返回值；
    名称允许重复，不作为会话定位键。
    """
    if not isinstance(name, str):
        raise ValueError(f"会话名必须是字符串: {name!r}")

    normalized = _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFKC", name)).strip()
    if not normalized:
        raise ValueError(f"会话名不合法（不能为空，长度 1~{SESSION_NAME_MAX_CHARS}）: {name!r}")
    if len(normalized) > SESSION_NAME_MAX_CHARS:
        raise ValueError(
            f"会话名过长（{len(normalized)} > {SESSION_NAME_MAX_CHARS}）: {normalized[:20]!r}…"
        )

    bad = sorted({c for c in normalized if unicodedata.category(c) in _SESSION_NAME_BANNED_CATEGORIES})
    if bad:
        # 报出**码点**：零宽空格这类字符肉眼看不见，不给出码点用户根本无从排查
        shown = "、".join(f"U+{ord(c):04X}({unicodedata.name(c, '?')})" for c in bad[:5])
        raise ValueError(
            f"会话名含不可见/控制字符: {shown}（可能来自复制粘贴，请重新输入）"
        )
    return normalized

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
    #: 客户端自定义的**标签**（如 "生产排查"）；空串 = 未命名。
    #: 纯给人识别用——**不唯一、不参与选中会话**（选中只认 session_id）。
    name: str = ""
    title: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    pending_approvals: int = 0
    message_count: int = 0
    #: 会话级固定主机；空串只用于 v7 以前的存量会话。
    host_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转为 api 层直接可序列化的结构。"""
        return {
            "session_id": self.session_id,
            "user": self.user,
            "name": self.name,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pending_approvals": self.pending_approvals,
            "message_count": self.message_count,
            "host_id": self.host_id or None,
        }


def is_owner(info: SessionInfo, identity: str | None) -> bool:
    """只判断身份归属；调用方另校验存在性与当前主机访问权。

    HTTP 必须传已验证身份并将不匹配映射为 404；None 仅供可信内部兼容调用。
    """
    return identity is None or info.user == identity


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
    """业务层聊天结果；HTTP 返回既有会话 ID，内部无存储调用可为 None。

    approval_id 供内部轮次管理，当前 /chat 响应仅含 reply、session_id、session_name。
    """

    reply: str
    session_id: str | None = None
    #: 本次使用的会话名（客户端自定义别名）；未命名时为 None
    session_name: str | None = None
    #: 本轮若触发写命令审批，这里是被中断那一轮的审批 id（`/approvals/{id}` 用）
    approval_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.reply,
            "session_id": self.session_id,
            "session_name": self.session_name,
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
    def create(
        self, *, user: str | None = "", title: str = "", name: str = "",
        host_id: str = ""
    ) -> SessionInfo:
        """新建一个会话并返回其元信息。

        **session_id 永远由存储层生成**——它是选中会话的**唯一**方式。

        `user` 是**会话的归属身份**（仅可信内部旧入口可传 `None`/空）。实现必须把 `None`
        归一成 `""` 再落库，否则会 `INSERT ... user=NULL` 并构造出 `SessionInfo(user=None)`——
        同一字段在读路径上却是 `str`（`_row_to_session` 有 `or ""` 兜底），类型漂移。

        `name` 是**给人看的标签**（用于在 `GET /sessions` 里认出是哪段对话），
        **不参与选中会话、不要求唯一**：同一个人可以有两段都叫「生产排查」的对话。
        别把它做成「按名字复用已有会话」——那等于把名字变回选择器，
        与「id 是唯一选择器」的设计冲突（调用方以为开了一段新对话，实际接到了旧的）。
        """

    @abstractmethod
    def get(self, session_id: str) -> SessionInfo | None:
        """取会话元信息；不存在返回 None。"""

    @abstractmethod
    def list(self, *, user: str | None = None, limit: int = 50, host_ids: set[str] | None = None) -> list[SessionInfo]:
        """列出会话，最近更新的在前。

        `user` 给定时**只返回该身份的会话**；`None` = 不筛
        （仅供可信内部维护；HTTP 始终传入身份和主机范围）。

        过滤必须下推到 **LIMIT 之前**（写进 SQL 的 WHERE）：先取 limit 条再过滤，
        会让结果少于 `limit`——调用方拿到的「最近 50 条」其实不是最近的 50 条。
        """

    @abstractmethod
    def delete(self, session_id: str, *, user: str | None = None) -> bool:
        """删除会话**连同其全部消息与审批决策记录**（同事务；不依赖外键）。

        `user` 给定时要求归属匹配，**不匹配则什么都不写、直接返回 False**。守卫必须在
        同一事务内、在**任何写之前**判定：先删 messages 再判归属，会让一次未授权的删除
        **抹掉别人的对话正文**而会话行还在。

        返回是否真的删掉了。调用方另需作废该会话的未决审批（见 ApprovalCancellable），
        且**必须在调用本方法之前**先用 `is_owner` 判一次——否则会先把别人的待审批写命令
        作废掉、再拿到 404，而 `cancel_pending` 是不可逆的。
        """

    @abstractmethod
    def rename(self, session_id: str, *, name: str, user: str | None = None) -> bool:
        """改会话标签。返回是否命中（不存在 / 归属不匹配 → False）。

        **不更新 `updated_at`**：改名不是「会话活动」。刷新它会让 `GET /sessions` 的排序
        和 TTL 的过期时机跟着一次纯标签操作跳动——那两处都该反映「最后一句话是什么时候说的」。
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

    def has_pending_approval(self, session_id: str, approval_id: str) -> bool:
        """确认已保存的待审批轮次，不受展示/模型窗口限制。

        未提供此校验的存储实现拒绝审批消费，不能把无法核对当作已保存。
        内置 SQLite 与内存存储均实现此方法；不改变 HTTP 或数据库 schema。
        """
        raise SessionError("会话存储不支持校验已保存的待审批轮次")

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
    def reconcile_pending(self, live_approval_ids: Collection[str], *, host_id: str | None = None) -> tuple[int, int]:
        """收敛不在 live_approval_ids 中的 pending，返回 (作废轮数, 结果未知轮数)。

        无持久决策标为 abandoned，有决策保守标为 unknown，不自动补执行。每轮含两条
        消息，计数按轮次。重启传空集合并省略 host_id；运行期按主机 TTL/容量淘汰后
        对账须指定 host_id，不能用单台主机的队列作废其它主机的申请。
        """

    @abstractmethod
    def record_decision(
        self,
        approval_id: str,
        *,
        session_id: str | None,
        action: str,
        decided_by: str = "",
        host_id: str = "",
        target_summary: str = "",
        actions_json: str = "[]",
        config_fingerprint: str = "",
    ) -> None:
        """在消费审批后、恢复执行前持久化决策；写失败必须上抛。

        此记录支撑重启后的 unknown 对账，不以可轮转日志代替。decided_by 是服务端
        身份，空串仅兼容历史或内部调用，HTTP 不允许匿名决策。
        """

    @abstractmethod
    def decided_approval_ids(self) -> set[str]:
        """返回已持久提交决策的审批 ID，供 pending 轮次对账。"""

    @abstractmethod
    def purge_expired(self) -> int:
        """清理过期 / 超容量的会话，连同它们的消息与审批决策记录，返回删除的会话数。

        由调用方节流后低频触发。

        **决策记录另有独立的过期路径**：`approval_decisions` 的保留期是
        `history_ttl_hours`（默认 7 天），由状态日志清理执行
        （见 `SqliteHistoryStore.purge_expired`）。本方法只负责「会话消失时顺手删掉
        它的决策记录」这条**快路径**——它按 session_id 删，扫不到 session_id 为空的行，
        也覆盖不到「会话被 DELETE 掉的孤儿行」，没有兜底职责。
        """


@runtime_checkable
class ApprovalLedger(Protocol):
    """Agent 的窄持久化接口：恢复执行前记录决策，审批淘汰后收敛 pending。

    仅暴露这两个有严格调用时机的方法，避免 Agent 依赖整个会话 Store。
    """

    def record_decision(
        self,
        approval_id: str,
        *,
        session_id: str | None,
        action: str,
        decided_by: str = "",
        host_id: str = "",
        target_summary: str = "",
        actions_json: str = "[]",
        config_fingerprint: str = "",
    ) -> None:
        ...

    def reconcile_pending(self, live_approval_ids: Collection[str], *, host_id: str | None = None) -> tuple[int, int]:
        ...


@runtime_checkable
class HistoryCapable(Protocol):
    """可选多轮协议；不支持时调用方退回 reply。

    使用独立 reply_turn 方法名，因为 runtime_checkable 不验证签名，给旧 reply
    增加关键字参数会误判既有实现。
    """

    def reply_turn(
        self, message: str, *, history: Sequence[ChatMessage] = ()
    ) -> TurnResult:
        """带着历史跑一轮，返回最终回答或待审批提示；失败时抛异常由调用方回落。"""
