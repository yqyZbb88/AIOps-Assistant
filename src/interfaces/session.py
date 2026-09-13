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

import re
import unicodedata
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
#: **结果未知**：决策已提交（命令可能已执行），但进程在「取走审批项」与「命令跑完」
#: 之间断了，无从确认。与 `abandoned`（确定没执行）**必须分开**——把这种标成「未执行」
#: 会让审计结论与事实相反。同样不进模型窗口（见 WINDOW_STATUSES）。
STATUS_UNKNOWN = "unknown"
STATUS_PENDING_APPROVAL = "pending_approval"
STATUS_ABANDONED = "abandoned"

# 每轮默认重发给模型的历史消息条数上限（限 token；实现与调用方共用同一个默认值）
DEFAULT_MESSAGE_WINDOW = 20

#: 会话名（客户端自定义别名）的格式。
#: **限制长度是硬要求**：不限的话有人会拿一兆字节当名字塞进唯一索引（长度按码点算）。
#: 名字**不等于** session_id——id 仍是服务端生成的 32 位随机值。
SESSION_NAME_MAX_CHARS = 64

#: 会话名**拒绝**的 Unicode 类别（而不是「只允许某几种文字」）：
#:   Cc 控制字符（NUL、ESC、换行…）——会污染日志、能注入终端转义
#:   Cf 格式字符（零宽空格 U+200B、BOM U+FEFF、RTL 覆盖 U+202E、软连字符…）——**肉眼看不见**
#: 其余**一律放行**：假名、重音字母、中文标点、emoji、阿拉伯文……都行。
#:
#: 为什么是「按类别拒」而不是「按字符白名单放行」：这两条是**有明确边界的类别**，
#: 排除它们是完备的，不会漏；而枚举「危险的字符」才会漏。白名单会顺带误伤
#: 大量完全正常的可见字符，却换不来额外的安全。
_SESSION_NAME_BANNED_CATEGORIES = frozenset({"Cc", "Cf"})

#: 连续空白（含换行/制表/不换行空格/全角空格）→ 单个普通空格
_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_session_name(name: str) -> str:
    """归一化并校验会话名，返回**归一化后**的名字；不合法抛 ValueError。

    **返回值可能与入参不同，调用方必须使用返回值**（响应里回带的也是它）。

    三步：

    1. **NFKC 规范化 + 折叠空白**：`ｔｅｓｔ１` → `test1`、全角空格 `U+3000` /
       不换行空格 `U+00A0` / 换行 / 制表符 → 普通空格 `U+0020`，连续空白折成一个并去首尾。
    2. **拒绝 `Cc` / `Cf` 两类字符**（控制字符与格式字符）。
    3. **限长** 1~64（按码点算）。

    为什么必须归一化：**空格有四种肉眼无法区分的编码**（普通 / 不换行 / 全角 / 零宽），
    而中文输入法下按空格键很可能打出全角空格 `U+3000`。不归一化的话，用户以为在续聊
    同一段对话，服务端却当成一个全新的名字；更糟的是他看这两行数据**长得一模一样**，
    没有任何线索可查。全角/半角字母同理。

    为什么归一化之后还要拒 `Cf`：**NFKC 消不掉零宽空格 `U+200B` 与 BOM `U+FEFF`**
    ——它们不是空白的变体，而是各自独立的字符，`\\s` 也不匹配它们。它们恰好是归一化
    **唯一治不了**的那类问题（真实场景：从聊天记录/网页里复制一个名字，很容易带上
    不可见字符，于是「复制粘贴的那次」与「手打的那次」落到两段不同的对话）。
    所以必须在这一层挡掉。

    **不限制可以用哪些文字**：只按类别排除，其余一律放行。
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
        }


def is_owner(info: SessionInfo, identity: str | None) -> bool:
    """调用者是否有权访问这个会话。

    - `identity is None` → **鉴权未启用**：没有身份可比，一律放行（行为与改造前一致）；
    - 否则要求 `info.user` **精确匹配**，取「一律拒绝」——`user` 为空的存量行
      **任何身份都不匹配**（空串不等于任何非空身份）。

    这是所有者判定的**唯一实现**，供 api 层（`/sessions/*`）与 core 层（`/chat` 的
    `_resolve_session`）共用；两处各写一遍必然漂移。

    **不匹配一律转 404**（不是 403）：与既有的「未知 id → 404」一致，且不泄露
    「这个 id 存在，只是不属于你」。

    注意它**只判归属**，不管「是否存在」——调用方要先自己处理 `None`。
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
    """一次 `/chat` 的结果：回答文本 + 会话 id。

    会话 id **恒回带**（新建时是新生成的，续聊时是原来的）：只在新建时回带的话，
    客户端与测试都得处理「有时有、有时没有」，是最容易漏传分支的形态。
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
        self, *, user: str | None = "", title: str = "", name: str = ""
    ) -> SessionInfo:
        """新建一个会话并返回其元信息。

        **session_id 永远由存储层生成**——它是选中会话的**唯一**方式。

        `user` 是**会话的归属身份**（鉴权关闭时传 `None`/空）。实现必须把 `None`
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
    def list(self, *, user: str | None = None, limit: int = 50) -> list[SessionInfo]:
        """列出会话，最近更新的在前。

        `user` 给定时**只返回该身份的会话**（鉴权开启时的形态）；`None` = 不筛
        （鉴权关闭，行为与改造前一致）。

        过滤必须下推到 **LIMIT 之前**（写进 SQL 的 WHERE）：先取 limit 条再过滤，
        会让结果少于 `limit`——调用方拿到的「最近 50 条」其实不是最近的 50 条。
        """

    @abstractmethod
    def delete(self, session_id: str, *, user: str | None = None) -> bool:
        """删除会话**连同其全部消息**（同事务；不依赖外键）。

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
    def reconcile_pending(self, live_approval_ids: Collection[str]) -> tuple[int, int]:
        """收敛**不在** live_approval_ids 里的 pending **轮**，返回 `(作废轮数, 结果未知轮数)`。

        两类要分开，**绝不能混为一谈**：

        - 该 `approval_id` **没有**决策记录 → 决策从没提交过，命令一定没执行 → `abandoned`；
        - **有**决策记录 → 决策已提交，但进程死在「取走审批项」与「命令跑完」之间 →
          命令**可能已经执行过** → `unknown`。

        第二种情况是审计结论问题而不是文字问题：标成「未执行」会让日志和事实相反，
        方向恰是 fail-safe 的反面（详见 `approval_decisions` 表与 `record_decision`）。

        返回**轮数**而非行数：一轮有 user + assistant 两行，数行会翻倍（与
        `pending_approvals` 的口径一致）。

        - 开库时传空集合：新进程里任何 pending 行必然是孤儿（审批存在进程内存里，
          不可能跨重启存活）；
        - 运行期传入 `approvals.list()` 的 id 集合：覆盖 TTL/容量淘汰造成的孤儿。
        """

    @abstractmethod
    def record_decision(self, approval_id: str, *, session_id: str | None, action: str) -> None:
        """落一条审批决策记录（`approval_decisions` 表）。

        **调用时机是这个方法的全部要害**：必须在 `decide()` 把待审批项 `pop()` 掉之后、
        `resume()` 执行命令**之前**。进程若恰好死在这两者之间，重启后
        `reconcile_pending` 才能据此把那一轮标成「结果未知」而不是错误的「未执行」——
        那是唯一能覆盖这个窗口的位置，而 core / api 层都只能在这个区间的外面动手。
        """

    @abstractmethod
    def decided_approval_ids(self) -> set[str]:
        """所有已提交过决策的 approval_id（供请求... 见 reconcile_pending 的判据）。"""

    @abstractmethod
    def purge_expired(self) -> int:
        """清理过期会话（连同消息）**与过期的决策记录**，返回删除的会话数。

        由调用方节流后低频触发。
        """


@runtime_checkable
class ApprovalLedger(Protocol):
    """审批与「会话里那一轮 pending」的对账口（窄接口）。

    **只为 OpsAssistant 存在**：它有两个动作的时机**只有 Agent 自己知道**，core / api
    都插不进去，所以用一个窄接口把这两件事托付给它，而不是把整个 `SessionStore` 递进去。

    - `record_decision`：必须在 `decide()` 的 `pop()` 与 `resume()` **之间**落盘。
      那是唯一能覆盖「决策已提交、命令可能已执行」风险窗口的位置——core 与 api 层
      都只能在这个区间的**外面**动手。
    - `reconcile_pending`：审批项被 TTL/容量淘汰时立刻把会话里那轮收敛掉。否则它会
      一直显示「有一条命令等你批」，而 `/approvals` 里根本没有它。

    两套 `SessionStore` 实现**结构上就已经满足**本协议（方法名与签名一致），无需显式
    继承——这正是用 Protocol 而非 ABC 的理由。
    """

    def record_decision(
        self, approval_id: str, *, session_id: str | None, action: str
    ) -> None:
        ...

    def reconcile_pending(self, live_approval_ids: Collection[str]) -> tuple[int, int]:
        ...


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
