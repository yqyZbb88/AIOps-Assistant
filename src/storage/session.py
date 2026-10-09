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
from contextlib import contextmanager
from typing import Any, Callable, Collection, Iterator

from src.interfaces.session import (
    DEFAULT_MESSAGE_WINDOW,
    ROLE_ASSISTANT,
    ROLE_USER,
    STATUS_ABANDONED,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
    STATUS_UNKNOWN,
    WINDOW_STATUSES,
    ChatMessage,
    SessionError,
    SessionNotFoundError,
    SessionInfo,
    SessionStore,
)
from src.storage.db import Database
from src.utils.time import cutoff_iso, to_iso
from src.utils.security import redact_text

logger = logging.getLogger("aiops_assistant.storage.session")

DEFAULT_TTL_SECONDS = 72 * 3600
DEFAULT_MAX_SESSIONS = 200
DEFAULT_PURGE_INTERVAL_SECONDS = 60
# 窗口默认值在 interfaces/session.py 定义，实现方与调用方共用同一个（避免两处漂移）

# 会话标题取首条用户消息的前若干字符（纯展示用）
_TITLE_MAX_CHARS = 40

# 会话查询的公共列：待审批数与消息数**实时统计**，不落成列。
# 存成列就得在 append_turn / update_pending / reconcile_pending 三处同步维护，
# 批量作废时极易漂移；实时统计则不可能算错。
_SESSION_COLUMNS = """
    s.session_id, s.user, s.name, s.title, s.status, s.created_at, s.updated_at, s.host_id,
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
    text = " ".join(redact_text(content or "").split())
    return text[:_TITLE_MAX_CHARS]


def _row_to_session(row: dict[str, Any]) -> SessionInfo:
    return SessionInfo(
        session_id=row["session_id"],
        user=row["user"] or "",
        name=row.get("name") or "",
        title=row["title"] or "",
        status=row["status"] or "active",
        created_at=row["created_at"] or "",
        updated_at=row["updated_at"] or "",
        pending_approvals=int(row["pending_approvals"] or 0),
        message_count=int(row["message_count"] or 0),
        host_id=row.get("host_id") or "",
    )


def _row_to_message(row: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=row["role"],
        content=redact_text(row["content"]),
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

    def _ok(self) -> None:
        """一次成功操作 → 标记健康。

        **必须成对调用**：`healthy` 反映的是「最近一次操作的结果」，而不是
        「历史上有没有失败过」。少了这一步，一次几秒的瞬时锁（典型成因是外部
        查看器占着库）就会让 `/health` **永远**显示 degraded，而库其实好好的——
        那样的字段会立刻失去可信度，比不报还糟。
        """
        self._healthy = True
        self._last_error = None

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        try:
            rows = self.db.query(sql, params)
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()
        return rows

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        """单条写语句 + 健康记账。"""
        try:
            rowcount = self.db.execute(sql, params)
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()
        return rowcount

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """写事务 + 健康记账（失败统一转成 SessionError）。"""
        try:
            with self.db.transaction() as conn:
                yield conn
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()

    # -- 会话生命周期 -------------------------------------------------------
    def create(
        self, *, user: str | None = "", title: str = "", name: str = "",
        host_id: str = ""
    ) -> SessionInfo:
        # 归一 None → ""：可信内部旧入口可传 None。不归一的话会 INSERT 成 NULL，
        # 并构造出 SessionInfo(user=None)，而读路径 _row_to_session 有 `or ""` 兜底——
        # 同一个字段两条路径两种类型。
        user = user or ""
        session_id = uuid.uuid4().hex
        stamp = to_iso(self._clock())
        self._execute(
            "INSERT INTO sessions "
            "(session_id, user, name, title, status, created_at, updated_at, host_id) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?, ?)",
            (session_id, user, name, title, stamp, stamp, host_id),
        )

        self._maybe_purge()
        return SessionInfo(
            session_id=session_id, user=user, name=name, title=title,
            status="active", created_at=stamp, updated_at=stamp, host_id=host_id,
        )

    def get(self, session_id: str) -> SessionInfo | None:
        rows = self._query(
            f"SELECT {_SESSION_COLUMNS} FROM sessions s WHERE s.session_id = ?", (session_id,)
        )
        return _row_to_session(rows[0]) if rows else None


    def list(self, *, user: str | None = None, limit: int = 50, host_ids: set[str] | None = None) -> list[SessionInfo]:
        # 次键 rowid 不可省：时间戳是秒级精度，同一秒内建的两个会话 updated_at 完全相同，
        # 只按 updated_at 排序时顺序不确定（测试会随机翻车）。
        #
        # user 条件写进 SQL 而不是取回来再过滤：先取 limit 条再筛会让「最近 50 条」
        # 其实不是最近的 50 条。
        where, params = "", []
        if user is not None:
            where, params = "WHERE s.user = ? ", [user]
        if host_ids is not None:
            if not host_ids:
                return []
            where += ("AND " if where else "WHERE ") + "s.host_id IN (" + ",".join("?" for _ in host_ids) + ") "
            params.extend(sorted(host_ids))
        rows = self._query(
            f"SELECT {_SESSION_COLUMNS} FROM sessions s "
            f"{where}"
            "ORDER BY s.updated_at DESC, s.rowid DESC LIMIT ?",
            (*params, max(1, int(limit))),
        )
        return [_row_to_session(r) for r in rows]

    def delete(self, session_id: str, *, user: str | None = None) -> bool:
        """删会话连同其消息与审批决策记录（同事务，不依赖外键）。

        归属守卫必须在**任何写之前**、在同一事务内判定：先删 messages 再判归属，
        一次未授权的删除就会抹掉别人的对话正文，而会话行还在。
        """
        with self._tx() as conn:
            row = conn.execute(
                "SELECT user FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return False
            if user is not None and (row["user"] or "") != user:
                return False  # 什么都不写
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            # 决策记录跟着会话走（理由同 purge_expired）。这里**必须显式删**：决策表没有外键，
            # 也没人会因为会话消失而回头清理它；不删就留下一批 session_id 指向不存在会话的
            # 孤儿行，只能等状态日志清理按 TTL 兜底扫掉。
            conn.execute("DELETE FROM approval_decisions WHERE session_id = ?", (session_id,))
            cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            deleted = cur.rowcount > 0
        return deleted

    def rename(self, session_id: str, *, name: str, user: str | None = None) -> bool:
        """改会话标签；**不碰 `updated_at`**（见 ABC 的说明）。

        单条 UPDATE 本身就是原子的，不需要显式事务。
        """
        sql = "UPDATE sessions SET name = ? WHERE session_id = ?"
        params: tuple = (name, session_id)
        if user is not None:
            sql += " AND user = ?"
            params += (user,)
        return self._execute(sql, params) > 0

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
        """取最近的消息供展示（旧→新），含 pending / abandoned。"""
        rows = self._query(
            f"SELECT {_MESSAGE_COLUMNS} FROM ("
            f"SELECT {_MESSAGE_COLUMNS} FROM messages WHERE session_id = ? "
            "ORDER BY seq DESC LIMIT ?) ORDER BY seq ASC",
            (session_id, max(1, int(limit))),
        )
        return [_row_to_message(r) for r in rows]

    def has_pending_approval(self, session_id: str, approval_id: str) -> bool:
        return bool(self._query(
            "SELECT 1 FROM messages WHERE session_id = ? AND approval_id = ? "
            "AND status = ? AND role = ? LIMIT 1",
            (session_id, approval_id, STATUS_PENDING_APPROVAL, ROLE_ASSISTANT),
        ))

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
        with self._tx() as conn:
            # 必须与消息写入共用事务；独立 get 无法阻止并发删除留下孤立消息。
            if conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone() is None:
                raise SessionNotFoundError(f"会话不存在: {session_id}")
            conn.execute(
                _INSERT_MESSAGE,
                (session_id, session_id, ROLE_USER, redact_text(user_content), status, approval_id, stamp),
            )
            conn.execute(
                _INSERT_MESSAGE,
                (session_id, session_id, ROLE_ASSISTANT, redact_text(assistant_content), status, approval_id, stamp),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ?, "
                "title = CASE WHEN title = '' THEN ? ELSE title END "
                "WHERE session_id = ?",
                (stamp, _title_from(user_content), session_id),
            )

    def update_pending(
        self,
        approval_id: str,
        *,
        content: str,
        status: str = STATUS_FINAL,
        next_approval_id: str | None = None,
    ) -> bool:
        """按 approval_id 把 pending 行推进到最终态 / 链式下一跳 / 作废。

        命中的是**整轮**（user + assistant 两行共享 approval_id）；只有 assistant 行的
        content 被替换，user 行的原文保持不动。
        """
        stamp = to_iso(self._clock())
        # 只有链式审批才换 approval_id；最终态与作废都保留原 id 作为历史痕迹
        stored_id = next_approval_id if status == STATUS_PENDING_APPROVAL else approval_id
        with self._tx() as conn:
            row = conn.execute(
                "SELECT session_id FROM messages WHERE approval_id = ? AND status = ? LIMIT 1",
                (approval_id, STATUS_PENDING_APPROVAL),
            ).fetchone()
            if row is None:
                return False
            session_id = row[0]
            conn.execute(
                "UPDATE messages SET status = ?, approval_id = ?, "
                "content = CASE WHEN role = ? THEN ? ELSE content END "
                "WHERE approval_id = ? AND status = ?",
                (status, stored_id, ROLE_ASSISTANT, redact_text(content),
                 approval_id, STATUS_PENDING_APPROVAL),
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (stamp, session_id),
            )
        return True

    def reconcile_pending(self, live_approval_ids: Collection[str]) -> tuple[int, int]:
        """收敛不在 live 里的 pending 轮，返回 `(作废轮数, 结果未知轮数)`。

        两类的判据是「**有没有决策记录**」，不能混：没有 → 命令一定没执行（abandoned）；
        有 → 决策提交过，命令可能已执行（unknown）。反过来标就是审计结论错误。
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

        decided = self.decided_approval_ids()
        with self._tx() as conn:
            orphans = [
                r[0] for r in conn.execute(
                    f"SELECT DISTINCT approval_id FROM messages WHERE {cond}", cond_params
                ).fetchall()
            ]
            risky = sorted(a for a in orphans if a in decided)
            unknown_turns = 0
            if risky:
                # ① 提交过决策的 → unknown。必须**先**处理它们：改完之后它们不再满足 cond，
                #    下面那条 UPDATE 就只会扫到真正没提交过决策的。
                ph = ",".join("?" for _ in risky)
                unknown_turns = conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE {cond} "
                    f"AND approval_id IN ({ph}) AND role = ?",
                    (*cond_params, *risky, ROLE_ASSISTANT),
                ).fetchone()[0]
                conn.execute(
                    f"UPDATE messages SET status = ? WHERE {cond} AND approval_id IN ({ph})",
                    (STATUS_UNKNOWN, *cond_params, *risky),
                )
            # ② 剩下的 → abandoned（决策从没提交过，命令一定没执行）
            abandoned_turns = conn.execute(
                f"SELECT COUNT(*) FROM messages WHERE {cond} AND role = ?",
                (*cond_params, ROLE_ASSISTANT),
            ).fetchone()[0]
            conn.execute(
                f"UPDATE messages SET status = ? WHERE {cond}",
                (STATUS_ABANDONED, *cond_params),
            )
        return int(abandoned_turns), int(unknown_turns)

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
        """落一条审批决策记录（含决策人）。调用时机是全部要害，见 ABC 的说明。"""
        self._execute(
            "INSERT INTO approval_decisions "
            "(approval_id, session_id, action, decided_at, decided_by, host_id, "
            "target_summary, actions_json, config_fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                approval_id, session_id, action, to_iso(self._clock()), decided_by or "",
                host_id, target_summary, actions_json, config_fingerprint,
            ),
        )

    def decided_approval_ids(self) -> set[str]:
        return {r["approval_id"] for r in self._query(
            "SELECT DISTINCT approval_id FROM approval_decisions"
        )}

    # -- 清理 ---------------------------------------------------------------
    def purge_expired(self) -> int:
        """删掉过期会话（连同消息），再按容量上限淘汰最旧的若干个。"""
        cutoff = cutoff_iso(self._clock(), self.ttl_seconds)
        with self._tx() as conn:
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
                # 决策记录跟着会话一起走：它只为「重启后判 unknown」而存在，
                # 会话没了就没人再读它。
                #
                # 这是**快路径**，不是唯一路径：只按 session_id 删，扫不到 session_id 为空
                # 的行与「会话已被 DELETE」的孤儿行；而且本方法只在**新建会话**时被节流触发，
                # 长期不建新会话就不会跑。兜底过期由状态日志清理按 history_ttl_hours 执行
                # （见 storage/history.py::purge_expired）——两处都留，别当成重复删掉。
                conn.execute(
                    f"DELETE FROM approval_decisions WHERE session_id IN ({marks})", ids
                )
                conn.execute(f"DELETE FROM sessions WHERE session_id IN ({marks})", ids)

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
        #: 已提交的审批决策：approval_id → session_id（后者用于随会话清理）
        self._decisions: dict[str, str | None] = {}

    def _touch(self, session_id: str) -> None:
        """把会话标记为最近使用（OrderedDict 尾部 = 最新）。"""
        self._sessions.move_to_end(session_id)

    def create(
        self, *, user: str | None = "", title: str = "", name: str = "",
        host_id: str = ""
    ) -> SessionInfo:
        user = user or ""  # 归一 None → ""，与 SQLite 版一致（见 ABC 的说明）
        session_id = uuid.uuid4().hex
        stamp = to_iso(self._clock())
        self._sessions[session_id] = {
            "session_id": session_id, "user": user, "name": name, "title": title,
            "status": "active", "created_at": stamp, "updated_at": stamp,
            "host_id": host_id,
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

    def list(self, *, user: str | None = None, limit: int = 50, host_ids: set[str] | None = None) -> list[SessionInfo]:
        # OrderedDict 尾部 = 最新，故 reversed 即「最近更新的在前」。
        # 过滤放在切片**之前**，与 SQLite 版把条件写进 WHERE（而非取回来再筛）同理：
        # 否则「最近 50 条」会少于 50 条。
        rows = list(reversed(self._sessions.items()))
        if user is not None:
            rows = [(sid, row) for sid, row in rows if row.get("user", "") == user]
        if host_ids is not None:
            rows = [(sid, row) for sid, row in rows if row.get("host_id", "") in host_ids]
        infos = [self._info(sid) for sid, _ in rows]
        return [i for i in infos if i is not None][: max(1, int(limit))]

    def delete(self, session_id: str, *, user: str | None = None) -> bool:
        row = self._sessions.get(session_id)
        if row is None:
            return False
        if user is not None and row.get("user", "") != user:
            return False  # 什么都不删
        self._messages.pop(session_id, None)
        # 决策记录跟着会话走（与 SQLite 版一致）
        for approval_id in [a for a, sid in self._decisions.items() if sid == session_id]:
            self._decisions.pop(approval_id, None)
        self._sessions.pop(session_id, None)
        return True

    def rename(self, session_id: str, *, name: str, user: str | None = None) -> bool:
        """改标签；**不碰 `created_at`/`updated_at`**（顺序即 updated_at，见 ABC 的说明）。"""
        row = self._sessions.get(session_id)
        if row is None:
            return False
        if user is not None and row.get("user", "") != user:
            return False
        row["name"] = name
        return True

    def history(self, session_id: str, *, limit: int = DEFAULT_MESSAGE_WINDOW) -> list[ChatMessage]:
        visible = [m for m in self._messages.get(session_id, []) if m.status in WINDOW_STATUSES]
        return visible[-max(1, int(limit)):]

    def messages(self, session_id: str, *, limit: int = 100) -> list[ChatMessage]:
        return self._messages.get(session_id, [])[-max(1, int(limit)):]

    def has_pending_approval(self, session_id: str, approval_id: str) -> bool:
        return any(message.status == STATUS_PENDING_APPROVAL
                   and message.role == ROLE_ASSISTANT and message.approval_id == approval_id
                   for message in self._messages.get(session_id, ()))

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
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        stamp = to_iso(self._clock())
        msgs = self._messages.setdefault(session_id, [])
        # 两行共享 status / approval_id（理由见 SQLite 版 append_turn 的 docstring）
        msgs.append(ChatMessage(ROLE_USER, redact_text(user_content), len(msgs) + 1, status, approval_id, stamp))
        msgs.append(ChatMessage(ROLE_ASSISTANT, redact_text(assistant_content), len(msgs) + 1, status, approval_id, stamp))
        row = self._sessions[session_id]
        row["updated_at"] = stamp
        if not row["title"]:
            row["title"] = _title_from(user_content)
        self._touch(session_id)

    def update_pending(
        self,
        approval_id: str,
        *,
        content: str,
        status: str = STATUS_FINAL,
        next_approval_id: str | None = None,
    ) -> bool:
        stored_id = next_approval_id if status == STATUS_PENDING_APPROVAL else approval_id
        for session_id, msgs in self._messages.items():
            if not any(
                m.approval_id == approval_id and m.status == STATUS_PENDING_APPROVAL
                for m in msgs
            ):
                continue
            for idx, msg in enumerate(msgs):
                if msg.approval_id == approval_id and msg.status == STATUS_PENDING_APPROVAL:
                    # 命中整轮；只替换 assistant 行的 content
                    new_content = redact_text(content) if msg.role == ROLE_ASSISTANT else msg.content
                    msgs[idx] = ChatMessage(
                        msg.role, new_content, msg.seq, status, stored_id, msg.created_at
                    )
            self._sessions[session_id]["updated_at"] = to_iso(self._clock())
            self._touch(session_id)
            return True
        return False

    def reconcile_pending(self, live_approval_ids: Collection[str]) -> tuple[int, int]:
        live = {str(x) for x in live_approval_ids}
        abandoned = unknown = 0
        for msgs in self._messages.values():
            for idx, msg in enumerate(msgs):
                if msg.status != STATUS_PENDING_APPROVAL:
                    continue
                if msg.approval_id is not None and msg.approval_id in live:
                    continue
                # 有决策记录 = 决策提交过、命令可能已执行 → unknown；否则一定没执行
                status = (
                    STATUS_UNKNOWN
                    if msg.approval_id in self._decisions
                    else STATUS_ABANDONED
                )
                msgs[idx] = ChatMessage(
                    msg.role, msg.content, msg.seq, status, msg.approval_id, msg.created_at
                )
                if msg.role == ROLE_ASSISTANT:  # 按轮计数，不按行
                    if status == STATUS_UNKNOWN:
                        unknown += 1
                    else:
                        abandoned += 1
        return abandoned, unknown

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
        # 只留判「有没有提交过决策」需要的部分——内存实现是降级用的，不追求与库等价
        self._decisions[approval_id] = session_id

    def decided_approval_ids(self) -> set[str]:
        return set(self._decisions)

    def purge_expired(self) -> int:
        cutoff = cutoff_iso(self._clock(), self.ttl_seconds)
        doomed = [sid for sid, row in self._sessions.items() if row["updated_at"] < cutoff]
        # 容量上限：OrderedDict 头部 = 最旧
        alive = [sid for sid in self._sessions if sid not in doomed]
        if len(alive) > self.max_sessions:
            doomed.extend(alive[: len(alive) - self.max_sessions])
        ordered = list(dict.fromkeys(doomed))
        for sid in ordered:
            self._messages.pop(sid, None)
            self._sessions.pop(sid, None)
        # 决策记录跟着会话一起走（见 SQLite 版同一处的说明）
        if ordered:
            gone = set(ordered)
            for aid in [a for a, sid in self._decisions.items() if sid in gone]:
                self._decisions.pop(aid, None)
        return len(set(doomed))
