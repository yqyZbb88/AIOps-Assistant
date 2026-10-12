"""SQLite 连接工厂、显式事务和 schema 管理，不包含业务编排。

每操作新建并关闭连接，保留线程检查，避免跨线程共享事务。BEGIN IMMEDIATE
减少读后升级写的争用，锁等待之外只做有限重试。WAL 不可用时退回 DELETE。
父子数据由 Store 在同一事务显式维护；user_version 高于代码支持时拒绝开库。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

logger = logging.getLogger("aiops_assistant.storage.db")

# 新迁移追加版本分支并递增版本；保留旧分支以支持存量数据库。
# v1：基础六表；v2：会话名称及唯一索引；v3：名称改为可重名标签，移除唯一索引。
# v4：持久记录决策人，避免依赖会轮转的日志；v5：规则幂等抢占；v6：异常巡检。
# v7：多主机归属与统一动作执行审计。
SCHEMA_VERSION = 7

# 建表语句（幂等）。顺序即执行顺序。
_DDL: tuple[str, ...] = (
    """
    -- `name` 刻意排在**最后**：它由 v2 的 ALTER TABLE 追加，而 ALTER 只能加在末尾。
    -- 这样「全新建库」与「v1 迁移上来」两条路径的**列序也一致**（有测试锁结构指纹）。
    -- 若把它插在 user 后面，两条路径的列序会不同——所有查询都显式写列名所以功能无碍，
    -- 但将来有人写 `INSERT INTO sessions VALUES (...)` 就会在新库上正常、在旧库上炸。
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        user       TEXT NOT NULL DEFAULT '',
        title      TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        name       TEXT NOT NULL DEFAULT '',
        host_id    TEXT NOT NULL DEFAULT ''
    )
    """,
    # 注：sessions.name **没有**唯一索引——它是给人看的标签，不参与选中会话
    # （选中只认 session_id），因此允许重名。v2 曾有过一个 (user, name) 的部分唯一索引，
    # v3 已撤掉，见 _migrate。
    """
    CREATE TABLE IF NOT EXISTS messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  TEXT NOT NULL,
        seq         INTEGER NOT NULL,
        role        TEXT NOT NULL,
        content     TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'final',
        approval_id TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE (session_id, seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages (session_id, seq)",
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_key   TEXT NOT NULL,
        alertname   TEXT NOT NULL,
        severity    TEXT NOT NULL DEFAULT '',
        instance    TEXT NOT NULL DEFAULT '',
        summary     TEXT NOT NULL DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'firing',
        starts_at   TEXT NOT NULL DEFAULT '',
        ends_at     TEXT NOT NULL DEFAULT '',
        received_at TEXT NOT NULL,
        host_id     TEXT NOT NULL DEFAULT '',
        host_resolution TEXT NOT NULL DEFAULT 'unknown',
        UNIQUE (alert_key, starts_at, status)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_alerts_received ON alerts (received_at)",
    """
    CREATE TABLE IF NOT EXISTS remediations (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_key    TEXT NOT NULL,
        alertname    TEXT NOT NULL,
        severity     TEXT NOT NULL DEFAULT '',
        instance     TEXT NOT NULL DEFAULT '',
        matched_rule TEXT NOT NULL DEFAULT '',
        action       TEXT,
        status       TEXT NOT NULL DEFAULT '',
        detail       TEXT NOT NULL DEFAULT '',
        executed_at  TEXT NOT NULL,
        host_id      TEXT NOT NULL DEFAULT '',
        execution_id TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_remediations_key ON remediations (alert_key)",
    """
    CREATE TABLE IF NOT EXISTS ai_advice (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_key   TEXT NOT NULL,
        alertname   TEXT NOT NULL,
        severity    TEXT NOT NULL DEFAULT '',
        instance    TEXT NOT NULL DEFAULT '',
        advice      TEXT NOT NULL DEFAULT '',
        analyzed_at TEXT NOT NULL,
        host_id     TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ai_advice_key ON ai_advice (alert_key)",
    """
    CREATE TABLE IF NOT EXISTS approval_decisions (
        approval_id TEXT NOT NULL,
        session_id  TEXT,
        action      TEXT NOT NULL,
        decided_at  TEXT NOT NULL,
        -- `decided_by` 刻意排在**最后**：它由 v4 的 ALTER TABLE 追加，而 ALTER 只能加在
        -- 末尾。这样「全新建库」与「逐级迁移上来」两条路径的**列序也一致**（有结构指纹测试）。
        -- 空串 = 当时鉴权未启用（没有身份可言）——这一条本身就是审计要回答的问题，别用 NULL 含糊它。
        decided_by  TEXT NOT NULL DEFAULT '',
        host_id     TEXT NOT NULL DEFAULT '',
        target_summary TEXT NOT NULL DEFAULT '',
        actions_json TEXT NOT NULL DEFAULT '[]',
        config_fingerprint TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS remediation_claims (
        alert_key    TEXT NOT NULL,
        action       TEXT NOT NULL,
        alert_status TEXT NOT NULL,
        starts_at    TEXT NOT NULL,
        claimed_at   TEXT NOT NULL,
        host_id      TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (alert_key, action, alert_status, starts_at)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_remediation_claims_time ON remediation_claims (claimed_at)",
    """
    CREATE TABLE IF NOT EXISTS inspection_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        checked_at    TEXT NOT NULL,
        level         TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        findings_json TEXT NOT NULL,
        error_text    TEXT NOT NULL DEFAULT '',
        host_id       TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_inspection_runs_checked ON inspection_runs (checked_at)",
    """
    CREATE TABLE IF NOT EXISTS action_executions (
        execution_id       TEXT PRIMARY KEY,
        channel            TEXT NOT NULL,
        idempotency_key    TEXT NOT NULL UNIQUE,
        host_id            TEXT NOT NULL,
        action_name        TEXT NOT NULL,
        params_json        TEXT NOT NULL DEFAULT '{}',
        target_summary     TEXT NOT NULL DEFAULT '',
        config_fingerprint TEXT NOT NULL DEFAULT '',
        approval_id        TEXT NOT NULL DEFAULT '',
        alert_key          TEXT NOT NULL DEFAULT '',
        status             TEXT NOT NULL,
        detail             TEXT NOT NULL DEFAULT '',
        created_at         TEXT NOT NULL,
        started_at         TEXT NOT NULL DEFAULT '',
        finished_at        TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_action_executions_host_time "
    "ON action_executions (host_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_action_executions_approval "
    "ON action_executions (approval_id)",
)

# 建表 / 迁移的进程级锁。正常读写路径不碰它。
_init_lock = threading.Lock()

# busy timeout 外再做有限退避，仅重试 locked/busy，避免瞬时争用直接丢失本轮写入。
# 每次操作仍有自己的锁等待；总耗时可能超过单次 timeout，不重试任意 SQLite 错误。
_TRANSIENT_RETRY_DELAYS: tuple[float, ...] = (0.2, 0.5)


def _is_transient(exc: sqlite3.Error) -> bool:
    """是否属于「等一会儿可能就好了」的瞬时错误。"""
    message = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message
    )


class DatabaseError(Exception):
    """存储层不可用（开库失败 / 版本过新 / 迁移失败）。"""


def _retry_transient(op: Callable[[], Any], what: str) -> Any:
    """执行 op；遇到瞬时锁错误就退避重试，其它错误立即抛出。

    见 `_TRANSIENT_RETRY_DELAYS` 的说明（为什么 busy_timeout 之外还要这一层）。
    重试耗尽的最后一个异常会**原样抛出**——由调用方决定降级。
    """
    delays = (0.0, *_TRANSIENT_RETRY_DELAYS)
    last: sqlite3.Error | None = None
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            return op()
        except sqlite3.Error as exc:
            if not _is_transient(exc):
                raise  # 不是瞬时错误，重试没有意义
            last = exc
            logger.warning(
                "数据库瞬时锁定（%s），剩余重试 %d 次: %s",
                what.split()[0], len(delays) - index - 1, exc,
            )
    assert last is not None  # 循环至少跑一次，能走到这里必然有异常
    raise last


class Database:
    """一个 sqlite 文件的连接工厂 + schema 管理。

    实例本身很轻（只存路径），可被多个 store 共享；**不要把 Connection 存成实例属性**。
    """

    def __init__(self, path: str | Path, *, timeout: float = 5.0) -> None:
        self.path = Path(path)
        self.timeout = timeout
        self.journal_mode = "unknown"

    # -- 连接 ---------------------------------------------------------------
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开短连接并在退出时关闭；默认自动提交，多语句原子写使用 transaction。"""
        conn = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {max(0, int(self.timeout * 1000))}")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE 后提交或回滚；只重试取锁，事务体执行一次。"""
        with self.connect() as conn:
            _retry_transient(lambda: conn.execute("BEGIN IMMEDIATE"), "BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()

    # -- 便捷读写 -----------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """执行查询，返回 dict 列表。"""
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行自动提交的单条写语句，返回影响行数；仅瞬时锁错误有限重试。"""
        with self.connect() as conn:
            return _retry_transient(lambda: conn.execute(sql, params), sql).rowcount

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> int:
        """同一事务批量写；任何一条失败即整批回滚，返回受影响行数。

        仅 BEGIN 取锁可重试；逐条自动提交后重发整批会重复已经成功的写入。
        """
        with self.transaction() as conn:
            return conn.executemany(sql, seq).rowcount

    # -- 维护 ---------------------------------------------------------------
    def checkpoint(self) -> None:
        """把 WAL 内容合并回主库并截断 WAL。

        删除行**不会**让 WAL 自动缩小，需低频调用本方法（由 purge 顺带触发）。
        非 WAL 模式下是空操作。
        """
        if self.journal_mode != "wal":
            return
        try:
            with self.connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:  # 清理失败不影响主流程
            logger.debug("WAL checkpoint 失败: %s", exc)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Database {self.path} journal={self.journal_mode}>"


# ---------------------------------------------------------------------------
# 打开 / 初始化
# ---------------------------------------------------------------------------
def _apply_journal_mode(conn: sqlite3.Connection) -> str:
    """尝试开启 WAL；失败则回落 DELETE（**不**让持久化整体失效）。"""
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        return str(mode).lower()
    except sqlite3.Error as exc:
        logger.warning(
            "开启 WAL 失败（同步盘 / 网络盘常见），回落 journal_mode=DELETE: %s", exc
        )
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.Error:
            pass
        return "delete"


def _migrate(conn: sqlite3.Connection, current: int) -> None:
    """按历史版本分支迁移到 SCHEMA_VERSION，新迁移追加分支并保留旧分支。

    新库走完整 DDL，旧库逐版本升级，两条路径应得到一致结构；既有迁移含索引调整。
    """
    if current == 0:
        for statement in _DDL:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return

    if current < 2:
        # v1 → v2：会话名（当时是「别名」，能直接选中会话）
        conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_user_name "
            "ON sessions (user, name) WHERE name <> ''"
        )

    if current < 3:
        # v2 → v3：撤掉唯一索引。名字降级为「纯标签」后不再有唯一性需求——
        # 它不参与选中会话，而强制唯一只会让人开第二段同名对话时被迫改名。
        # 注意 v1 直接升到 v3 时这里也会跑（上面那个索引刚建出来就被撤掉）：
        # **每个历史分支都得保留、按序执行**，这样任意旧版本都能升到最新，
        # 且「全新建库」与「逐级迁移」得到同一结构（有测试锁结构指纹）。
        conn.execute("DROP INDEX IF EXISTS idx_sessions_user_name")

    if current < 4:
        # v3 → v4：审批决策记名。只写审计日志不够——日志会轮转被冲掉，
        # 而这张表要留住「那条命令是谁批的」，尤其当它落进 `unknown`（结果未知）时。
        conn.execute(
            "ALTER TABLE approval_decisions ADD COLUMN decided_by TEXT NOT NULL DEFAULT ''"
        )

    if current < 5:
        # v4 → v5：自动处置执行前的持久化幂等抢占。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS remediation_claims (
                alert_key    TEXT NOT NULL,
                action       TEXT NOT NULL,
                alert_status TEXT NOT NULL,
                starts_at    TEXT NOT NULL,
                claimed_at   TEXT NOT NULL,
                PRIMARY KEY (alert_key, action, alert_status, starts_at)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_remediation_claims_time "
            "ON remediation_claims (claimed_at)"
        )

    if current < 6:
        # v5 → v6：固定指标巡检异常归档。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inspection_runs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at    TEXT NOT NULL,
                level         TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                findings_json TEXT NOT NULL,
                error_text    TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inspection_runs_checked "
            "ON inspection_runs (checked_at)"
        )

    if current < 7:
        # v6 → v7：所有会影响判断或执行的记录都显式携带主机归属。
        # 存量行保持空串（unknown），绝不按 instance 猜测回填。
        def add_column(table: str, definition: str) -> None:
            """允许从一次被中断的 v7 迁移安全重试。"""
            name = definition.split(maxsplit=1)[0]
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

        add_column("sessions", "host_id TEXT NOT NULL DEFAULT ''")
        add_column("alerts", "host_id TEXT NOT NULL DEFAULT ''")
        add_column("alerts", "host_resolution TEXT NOT NULL DEFAULT 'unknown'")
        add_column("remediations", "host_id TEXT NOT NULL DEFAULT ''")
        add_column("remediations", "execution_id TEXT NOT NULL DEFAULT ''")
        add_column("ai_advice", "host_id TEXT NOT NULL DEFAULT ''")
        add_column("approval_decisions", "host_id TEXT NOT NULL DEFAULT ''")
        add_column("approval_decisions", "target_summary TEXT NOT NULL DEFAULT ''")
        add_column("approval_decisions", "actions_json TEXT NOT NULL DEFAULT '[]'")
        add_column("approval_decisions", "config_fingerprint TEXT NOT NULL DEFAULT ''")
        add_column("remediation_claims", "host_id TEXT NOT NULL DEFAULT ''")
        add_column("inspection_runs", "host_id TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS action_executions (
                execution_id       TEXT PRIMARY KEY,
                channel            TEXT NOT NULL,
                idempotency_key    TEXT NOT NULL UNIQUE,
                host_id            TEXT NOT NULL,
                action_name        TEXT NOT NULL,
                params_json        TEXT NOT NULL DEFAULT '{}',
                target_summary     TEXT NOT NULL DEFAULT '',
                config_fingerprint TEXT NOT NULL DEFAULT '',
                approval_id        TEXT NOT NULL DEFAULT '',
                alert_key          TEXT NOT NULL DEFAULT '',
                status             TEXT NOT NULL,
                detail             TEXT NOT NULL DEFAULT '',
                created_at         TEXT NOT NULL,
                started_at         TEXT NOT NULL DEFAULT '',
                finished_at        TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_executions_host_time "
            "ON action_executions (host_id, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_executions_approval "
            "ON action_executions (approval_id)"
        )

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def open_database(path: str | Path, *, timeout: float = 5.0) -> Database | None:
    """初始化或打开数据库；失败记录日志并返回 None，由调用方按能力安全降级。"""
    db = Database(path, timeout=timeout)
    try:
        db.path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("无法创建数据库目录 %s，持久化关闭: %s", db.path.parent, exc)
        return None

    try:
        with _init_lock, closing(
            sqlite3.connect(str(db.path), timeout=timeout, isolation_level=None)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            db.journal_mode = _apply_journal_mode(conn)

            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                # 文件来自更新的代码：**绝不写入**，否则可能损坏对方的数据
                logger.error(
                    "数据库 schema 版本 %d 高于本代码支持的 %d（文件来自更新版本的代码），"
                    "持久化关闭以免写坏数据: %s",
                    version, SCHEMA_VERSION, db.path,
                )
                return None

            # 建表 + 迁移 + 写版本号作为一个原子事务。
            # 注意 isolation_level=None 下 `with conn:` 是空操作（不自动开事务），
            # 必须显式 BEGIN —— 否则建表成功但写版本号失败会留下「有表、版本仍是 0」的半成品。
            conn.execute("BEGIN IMMEDIATE")
            try:
                _migrate(conn, version)
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("打开数据库失败，持久化关闭: %s（%s）", db.path, exc)
        return None

    logger.info(
        "持久化已启用: %s（journal=%s, schema v%d）", db.path, db.journal_mode, SCHEMA_VERSION
    )
    return db
