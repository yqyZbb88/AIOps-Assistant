"""对话会话存储：SQLite 实现 + 内存实现（降级 / 测试用）。

契约见 src/interfaces/session.py::SessionStore。两条实现都遵守同一约定：
**写失败抛 SessionError（不静默吞）**，读失败同样抛出，由调用方决定如何降级。

几个刻意的取舍：
- `seq` 用**单条 SQL 原子分配**（`SELECT COALESCE(MAX(seq),0)+1` 作为 INSERT 的子查询），
  而不是「先 SELECT MAX 再 INSERT」。后者在并发下会做「读后升级写」，两个线程同时
  升级必有一个撞 SQLITE_BUSY，而 busy_timeout 对这种升级不可靠。
- 删会话时**显式同事务删消息**，不依赖外键——sqlite 的 foreign_keys 是**每连接**的
  pragma 且默认关闭，指望它等于指望一个不存在的约束。
- 清理（TTL + 容量）**惰性 + 节流**触发，不引入后台线程：每次写都全表扫太贵，
  而本项目写频率低，几十秒一次的延迟完全可接受。
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from collections import OrderedDict
from typing import Any, Callable, Collection

from src.interfaces.session import (
    ROLE_ASSISTANT,
    ROLE_USER,
    STATUS_ABANDONED,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
    WINDOW_STATUSES,
    ChatMessage,
    SessionError,
    SessionInfo,
    SessionStore,
)
from src.storage.db import Database
from src.utils.time import cutoff_iso, to_iso

logger = logging.getLogger("ops_agent.storage.session")

DEFAULT_TTL_SECONDS = 72 * 3600
DEFAULT_MAX_SESSIONS = 200
DEFAULT_MESSAGE_WINDOW = 20
DEFAULT_PURGE_INTERVAL_SECONDS = 60

# 会话标题取首条用户消息的前若干字符（纯展示用）
_TITLE_MAX_CHARS = 40

# 会话查询的公共列：待审批数与消息数**实时统计**，不落成列。
# 存成列就得在 append_turn / update_pending / reconcile_pending 三处同步维护，
# 批量作废时极易漂移；实时统计则不可能算错。
_SESSION_COLUMNS = """
    s.session_id, s.user, s.title, s.status, s.created_at, s.updated_at,
    (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id) AS message_count,
    (SELECT COUNT(*) FROM messages m
      WHERE m.session_id = s.session_id AND m.status = 'pending_approval'
        AND m.role = 'assistant') AS pending_approvals
"""

_MESSAGE_COLUMNS = "role, content, seq, status, approval_id, created_at"

# 单条 INSERT 里原子分配 seq（见模块 docstring）
_INSERT_MESSAGE = (
    "INSERT INTO messages (session_id, seq, role, content, status, approval_id, created_at) "
    "VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE session_id = ?), ?, ?, ?, ?, ?)"
)


def _title_from(content: str) -> str:
    """把首条用户消息截成会话标题。"""
    text = " ".join((content or "").split())
    return text[:_TITLE_MAX_CHARS]


def _row_to_session(row: dict[str, Any]) -> SessionInfo:
    return SessionInfo(
        session_id=row["session_id"],
        user=row["user"] or "",
        title=row["title"] or "",
        status=row["status"] or "active",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        pending_approvals=int(row["pending_approvals"] or 0),
        message_count=int(row["message_count"] or 0),
    )


def _row_to_message(row: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=row["role"],
        content=row["content"],
        seq=int(row["seq"]),
        status=row["status"],
        approval_id=row["approval_id"],
        created_at=row["created_at"] or "",
    )


class SqliteSessionStore(SessionStore):
    """会话存储的 SQLite 实现。"""

    def __init__(
        self,
        db: Database,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        clock: Callable[[], float] | None = None,
        purge_interval_seconds: float = DEFAULT_PURGE_INTERVAL_SECONDS,
    ) -> None:
        self.db = db
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self.max_sessions = max(int(max_sessions), 1)
        self.purge_interval_seconds = max(float(purge_interval_seconds), 0.0)
        self._clock: Callable[[], float] = clock or time.time
        self._healthy = True
        self._last_error: str | None = None
        self._last_purge = 0.0

    # -- 状态 ---------------------------------------------------------------
    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _fail(self, exc: Exception) -> SessionError:
        """记录失败并返回要抛出的异常（**不吞**，由调用方决定降级与否）。"""
        self._healthy = False
        self._last_error = str(exc)
        logger.error("会话存储操作失败: %s", exc, exc_info=True)
        return SessionError(str(exc))

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            return self.db.query(sql, params)
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc

    # -- 会话生命周期 -------------------------------------------------------
    def create(self, *, user: str = "", title: str = "") -> SessionInfo:
        session_id = uuid.uuid4().hex
        stamp = to_iso(self._clock())
        try:
            self.db.execute(
                "INSERT INTO sessions (session_id, user, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', ?, ?)",
                (session_id, user, title, stamp, stamp),
            )
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc

        self._maybe_purge()
        return SessionInfo(
            session_id=session_id, user=user, title=title,
            status="active", created_at=stamp, updated_at=stamp,
        )

    def get(self, session_id: str) -> SessionInfo | None:
        rows = self._query(
            f"SELECT {_SESSION_COLUMNS} FROM sessions s WHERE s.session_id = ?", (session_id,)
        )
        return _row_to_session(rows[0]) if rows else None

    def list(self, *, limit: int = 50) -> list[SessionInfo]:
        # 次键 rowid 不可省：时间戳是秒级精度，同一秒内建的两个会话 updated_at 完全相同，
        # 只按 updated_at 排序时顺序不确定（测试会随机翻车）。
        rows = self._query(
            f"SELECT {_SESSION_COLUMNS} FROM sessions s "
            "ORDER BY s.updated_at DESC, s.rowid DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        return [_row_to_session(r) for r in rows]

    def delete(self, session_id: str) -> bool:
        """删会话连同其消息（同事务，不依赖外键）。"""
        try:
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
                deleted = cur.rowcount > 0
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        return deleted

    # -- 消息 ---------------------------------------------------------------
    def history(self, session_id: str, *, limit: int = DEFAULT_MESSAGE_WINDOW) -> list[ChatMessage]:
        """取可进模型上下文的最近 limit 条（旧→新）。只含 final 状态。"""
        placeholders = ",".join("?" for _ in WINDOW_STATUSES)
        rows = self._query(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages "
            f"WHERE session_id = ? AND status IN ({placeholders}) "
            "ORDER BY seq DESC LIMIT ?",
            (session_id, *WINDOW_STATUSES, max(1, int(limit))),
        )
        return [_row_to_message(r) for r in reversed(rows)]

    def messages(self, session_id: str, *, limit: int = 100) -> list[ChatMessage]:
        """取全部状态的消息供展示（旧→新），含 pending / abandoned。"""
        rows = self._query(
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE session_id = ? "
            "ORDER BY seq ASC LIMIT ?",
            (session_id, max(1, int(limit))),
        )
        return [_row_to_message(r) for r in rows]

    def append_turn(
        self,
        session_id: str,
        *,
        user_content: str,
        assistant_content: str,
        status: str = STATUS_FINAL,
        approval_id: str | None = None,
    ) -> None:
        """同一事务写入某一轮的 user + assistant 两行（见接口 docstring 的硬要求）。

        **两行带同一个 status / approval_id**：中断那轮若只有 assistant 行是 pending，
        user 行仍是 final，下一轮窗口里就会出现一条没有回答的提问，模型会把旧问题
        当新问题再答一遍。整轮同进同出才是对的。
        """
        stamp = to_iso(self._clock())
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    _INSERT_MESSAGE,
                    (session_id, session_id, ROLE_USER, user_content, status, approval_id, stamp),
                )
                conn.execute(
                    _INSERT_MESSAGE,
                    (session_id, session_id, ROLE_ASSISTANT, assistant_content, status, approval_id, stamp),
                )
                conn.execute(
                    "UPDATE sessions SET updated_at = ?, "
                    "title = CASE WHEN title = '' THEN ? ELSE title END "
                    "WHERE session_id = ?",
                    (stamp, _title_from(user_content), session_id),
                )
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc

    def update_pending(
        self,
        session_id: str,
        approval_id: str,
        *,
        content: str,
        status: str = STATUS_FINAL,
        next_approval_id: str | None = None,
    ) -> bool:
        """把 pending 行推进到最终态 / 链式下一跳 / 作废。

        命中的是**整轮**（user + assistant 两行共享 approval_id）；只有 assistant 行的
        content 被替换，user 行的原文保持不动。
        """
        stamp = to_iso(self._clock())
        # 只有链式审批才换 approval_id；最终态与作废都保留原 id 作为历史痕迹
        stored_id = next_approval_id if status == STATUS_PENDING_APPROVAL else approval_id
        try:
            with self.db.transaction() as conn:
                cur = conn.execute(
                    "UPDATE messages SET status = ?, approval_id = ?, "
                    "content = CASE WHEN role = ? THEN ? ELSE content END "
                    "WHERE session_id = ? AND approval_id = ? AND status = ?",
                    (status, stored_id, ROLE_ASSISTANT, content,
                     session_id, approval_id, STATUS_PENDING_APPROVAL),
                )
                hit = cur.rowcount > 0
                if hit:
                    conn.execute(
                        "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                        (stamp, session_id),
                    )
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        return hit

    def reconcile_pending(self, live_approval_ids: Collection[str]) -> int:
        """把不在 live_approval_ids 里的 pending **轮**标为 abandoned，返回轮数。

        返回轮数而非行数：一轮有 user + assistant 两行，数行会翻倍
        （与 `pending_approvals` 的口径保持一致）。
        """
        live = tuple(str(x) for x in live_approval_ids)
        if not live:
            # 新进程里任何 pending 行必然是孤儿（审批存在进程内存，不可能跨重启存活）
            cond, cond_params = "status = ?", (STATUS_PENDING_APPROVAL,)
        else:
            placeholders = ",".join("?" for _ in live)
            cond = (
                "status = ? AND (approval_id IS NULL "
                f"OR approval_id NOT IN ({placeholders}))"
            )
            cond_params = (STATUS_PENDING_APPROVAL, *live)
        try:
            with self.db.transaction() as conn:
                turns = conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE {cond} AND role = ?",
                    (*cond_params, ROLE_ASSISTANT),
                ).fetchone()[0]
                conn.execute(
                    f"UPDATE messages SET status = ? WHERE {cond}",
                    (STATUS_ABANDONED, *cond_params),
                )
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        return int(turns)

    # -- 清理 ---------------------------------------------------------------
    def purge_expired(self) -> int:
        """删掉过期会话（连同消息），再按容量上限淘汰最旧的若干个。"""
        cutoff = cutoff_iso(self._clock(), self.ttl_seconds)
        try:
            with self.db.transaction() as conn:
                expired = [r[0] for r in conn.execute(
                    "SELECT session_id FROM sessions WHERE updated_at < ?", (cutoff,)
                ).fetchall()]
                overflow = [r[0] for r in conn.execute(
                    "SELECT session_id FROM sessions ORDER BY updated_at DESC LIMIT -1 OFFSET ?",
                    (self.max_sessions,),
                ).fetchall()]
                ids = list(dict.fromkeys([*expired, *overflow]))  # 去重且保序
                if ids:
                    marks = ",".join("?" for _ in ids)
                    conn.execute(f"DELETE FROM messages WHERE session_id IN ({marks})", ids)
                    conn.execute(f"DELETE FROM sessions WHERE session_id IN ({marks})", ids)
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc

        self._last_purge = self._clock()
        if ids:
            # WAL 只增不减，删行不会让它缩小；低频截断一次（事务外，非 WAL 下是空操作）
            self.db.checkpoint()
        return len(ids)

    def _maybe_purge(self) -> None:
        """按节流间隔触发清理。清理失败**不影响本轮写入**（写入已成功）。"""
        if self._clock() - self._last_purge < self.purge_interval_seconds:
            return
        try:
            self.purge_expired()
        except SessionError as exc:
            logger.warning("会话清理失败（不影响本轮写入）: %s", exc)


class InMemorySessionStore(SessionStore):
    """内存实现：供 SQLite 不可用时降级，以及单测使用。

    语义与 SQLite 版一致，只是**重启即丢**——这正是「降级」的含义：
    功能还在（多轮对话可用），只是不再持久。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self.max_sessions = max(int(max_sessions), 1)
        self._clock: Callable[[], float] = clock or time.time
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._messages: dict[str, list[ChatMessage]] = {}

    def _touch(self, session_id: str) -> None:
        """把会话标记为最近使用（OrderedDict 尾部 = 最新）。"""
        self._sessions.move_to_end(session_id)

    def create(self, *, user: str = "", title: str = "") -> SessionInfo:
        session_id = uuid.uuid4().hex
        stamp = to_iso(self._clock())
        self._sessions[session_id] = {
            "session_id": session_id, "user": user, "title": title,
            "status": "active", "created_at": stamp, "updated_at": stamp,
        }
        self._messages[session_id] = []
        self.purge_expired()
        return self.get(session_id)  # type: ignore[return-value]

    def _info(self, session_id: str) -> SessionInfo | None:
        row = self._sessions.get(session_id)
        if row is None:
            return None
        msgs = self._messages.get(session_id, [])
        return SessionInfo(
            **row,
            message_count=len(msgs),
            # 只数 assistant 行：一轮的两行共享 pending 状态，数全部会翻倍
            pending_approvals=sum(
                1 for m in msgs
                if m.status == STATUS_PENDING_APPROVAL and m.role == ROLE_ASSISTANT
            ),
        )

    def get(self, session_id: str) -> SessionInfo | None:
        return self._info(session_id)

    def list(self, *, limit: int = 50) -> list[SessionInfo]:
        infos = [self._info(sid) for sid in reversed(self._sessions)]
        return [i for i in infos if i is not None][: max(1, int(limit))]

    def delete(self, session_id: str) -> bool:
        self._messages.pop(session_id, None)
        return self._sessions.pop(session_id, None) is not None

    def history(self, session_id: str, *, limit: int = DEFAULT_MESSAGE_WINDOW) -> list[ChatMessage]:
        visible = [m for m in self._messages.get(session_id, []) if m.status in WINDOW_STATUSES]
        return visible[-max(1, int(limit)):]

    def messages(self, session_id: str, *, limit: int = 100) -> list[ChatMessage]:
        return self._messages.get(session_id, [])[: max(1, int(limit))]

    def append_turn(
        self,
        session_id: str,
        *,
        user_content: str,
        assistant_content: str,
        status: str = STATUS_FINAL,
        approval_id: str | None = None,
    ) -> None:
        if session_id not in self._sessions:
            raise SessionError(f"会话不存在: {session_id}")
        stamp = to_iso(self._clock())
        msgs = self._messages.setdefault(session_id, [])
        # 两行共享 status / approval_id（理由见 SQLite 版 append_turn 的 docstring）
        msgs.append(ChatMessage(ROLE_USER, user_content, len(msgs) + 1, status, approval_id, stamp))
        msgs.append(ChatMessage(ROLE_ASSISTANT, assistant_content, len(msgs) + 1, status, approval_id, stamp))
        row = self._sessions[session_id]
        row["updated_at"] = stamp
        if not row["title"]:
            row["title"] = _title_from(user_content)
        self._touch(session_id)

    def update_pending(
        self,
        session_id: str,
        approval_id: str,
        *,
        content: str,
        status: str = STATUS_FINAL,
        next_approval_id: str | None = None,
    ) -> bool:
        msgs = self._messages.get(session_id, [])
        stored_id = next_approval_id if status == STATUS_PENDING_APPROVAL else approval_id
        hit = False
        for idx, msg in enumerate(msgs):
            if msg.approval_id == approval_id and msg.status == STATUS_PENDING_APPROVAL:
                # 命中整轮；只替换 assistant 行的 content
                new_content = content if msg.role == ROLE_ASSISTANT else msg.content
                msgs[idx] = ChatMessage(
                    msg.role, new_content, msg.seq, status, stored_id, msg.created_at
                )
                hit = True
        if hit:
            self._sessions[session_id]["updated_at"] = to_iso(self._clock())
            self._touch(session_id)
        return hit

    def reconcile_pending(self, live_approval_ids: Collection[str]) -> int:
        live = {str(x) for x in live_approval_ids}
        changed = 0
        for session_id, msgs in self._messages.items():
            for idx, msg in enumerate(msgs):
                if msg.status != STATUS_PENDING_APPROVAL:
                    continue
                if msg.approval_id is not None and msg.approval_id in live:
                    continue
                msgs[idx] = ChatMessage(
                    msg.role, msg.content, msg.seq, STATUS_ABANDONED, msg.approval_id, msg.created_at
                )
                if msg.role == ROLE_ASSISTANT:  # 按轮计数，不按行
                    changed += 1
        return changed

    def purge_expired(self) -> int:
        cutoff = cutoff_iso(self._clock(), self.ttl_seconds)
        doomed = [sid for sid, row in self._sessions.items() if row["updated_at"] < cutoff]
        # 容量上限：OrderedDict 头部 = 最旧
        alive = [sid for sid in self._sessions if sid not in doomed]
        if len(alive) > self.max_sessions:
            doomed.extend(alive[: len(alive) - self.max_sessions])
        for sid in dict.fromkeys(doomed):
            self._messages.pop(sid, None)
            self._sessions.pop(sid, None)
        return len(set(doomed))
