"""SQLite 持久化底座：连接工厂 + 建表 + 版本校验。

本模块**只是底座**，不含业务语义；各存储实现（storage/session.py、storage/history.py）
在它之上写 SQL。分层上属叶子层，**不 import langchain**，core 也不直接用它。

设计要点（每一条都对应一个踩过的坑，改动前先读）：

1. **每操作新开连接**，`check_same_thread` 保持默认 True。
   sqlite 打开成本约 0.1~0.5ms，本项目只有 /chat 与 /alert 两个低频同步路由，可忽略；
   而共享一个连接跨线程用，风险不在性能而在**事务边界**——一个线程未提交的事务会被
   另一个线程的 commit 带走，且必须放弃 `check_same_thread` 这层护栏。
2. **必须 `with closing(...)` 关连接**：`with sqlite3.connect(p) as c:` 只提交/回滚事务，
   **不关连接**。漏一个 handle，在 Windows 上就是 `database is locked`、db 文件删不掉、
   或与 OneDrive 的同步冲突。
3. `isolation_level=None` + 显式 `BEGIN IMMEDIATE`。默认的隐式 deferred 事务在
   「先 SELECT 再 INSERT」时会做**读后升级写**，两个线程同时升级必有一个立刻 SQLITE_BUSY，
   而 busy_timeout 对这种升级不可靠。
4. **WAL 失败只回落，不关闭持久化**：仓库在 Desktop 下，OneDrive 常接管桌面，而
   网络盘/同步盘不支持 WAL 所需的共享内存。此时回落 `journal_mode=DELETE` 并记 warning。
5. **外键是假的**：`PRAGMA foreign_keys` 是**每连接**的且默认 OFF。本模块干脆不声明外键，
   删除父子数据时由调用方显式同事务删除（见 storage/session.py 的 delete）。
6. **版本双向校验**：`CREATE TABLE IF NOT EXISTS` 对「旧文件 + 新代码加列」是**静默**的
   （表已存在就什么都不做），之后 INSERT 才报 no such column，再被上层降级吞掉。
   所以用 `PRAGMA user_version` 主动比对：低于当前 → 迁移；**高于当前 → 拒绝使用**。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

logger = logging.getLogger("ops_agent.storage.db")

# 当前 schema 版本。改动 DDL 时**必须**递增，并在 _migrate 里补上追加式迁移。
#   v1 (2026-09-11 M4 第一批)：六张表
#   v2 (2026-09-11 M4 第二批)：sessions 加 name 列 + (user, name) 部分唯一索引
#   v3 (2026-09-12)：**撤掉**那个唯一索引——session_name 从「别名」降为「纯标签」，
#                    不再参与选中会话，因此没有唯一性需求（允许重名）
#   v4 (2026-09-13)：approval_decisions 加 decided_by 列。**决策人必须落库**，
#                    不能只写审计日志——日志会轮转（5 MiB × 3 份）而被冲掉，
#                    而这张表的 TTL 是 7 天：等你需要回答「那条 unknown 的命令是谁批的」时，
#                    磁盘上的日志早就转没了，库里那行却还在。
#   v5 (2026-09-15)：remediation_claims 自动处置幂等抢占表。
#   v6 (2026-09-15)：inspection_runs 固定指标巡检异常归档表。
SCHEMA_VERSION = 6

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
        name       TEXT NOT NULL DEFAULT ''
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
        executed_at  TEXT NOT NULL
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
        analyzed_at TEXT NOT NULL
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
        decided_by  TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS remediation_claims (
        alert_key    TEXT NOT NULL,
        action       TEXT NOT NULL,
        alert_status TEXT NOT NULL,
        starts_at    TEXT NOT NULL,
        claimed_at   TEXT NOT NULL,
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
        error_text    TEXT NOT NULL DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_inspection_runs_checked ON inspection_runs (checked_at)",
)

# 建表 / 迁移的进程级锁。正常读写路径不碰它。
_init_lock = threading.Lock()

#: 瞬时锁的退避重试间隔（秒）。
#:
#: 为什么 `busy_timeout` 之外还要这个：busy_timeout 只在**单个连接**内自旋重试，
#: 而 `database is locked` 最常见的成因是**另一个进程**（典型是一个开着读事务的
#: GUI 查看器）挡住了 WAL 的 checkpoint——写操作必须先 checkpoint 才能继续，
#: 于是持续拿不到锁。那类争用通常几秒内自解，退避重试能把「瞬时争用」与
#: 「真的坏了」分开，避免一次几秒的争用就丢掉一整轮会话。
#:
#: 只对 locked / busy 这类**瞬时**错误重试；其它 sqlite 错误立即抛出（重试没有意义）。
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
        """开一个连接；退出时**必定关闭**（见模块 docstring 第 2 条）。

        `isolation_level=None` → 关掉隐式事务，语句默认自动提交；
        需要原子性的多语句写入请用 transaction()。
        """
        conn = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式写事务：BEGIN IMMEDIATE → 正常提交 / 异常回滚。

        用 IMMEDIATE 而非默认的 deferred，避免「读后升级写」撞 SQLITE_BUSY
        （见模块 docstring 第 3 条）。

        `BEGIN` 本身带瞬时锁退避重试——**锁就是在这里取的**，所以重试放在这里
        就覆盖了整个事务。事务体只跑一次（不复跑调用方的代码块）。
        """
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
        """执行单条写语句（自动提交），返回受影响行数。瞬时锁会退避重试。

        单条语句重发是安全的：失败时什么都没写进去。
        """
        with self.connect() as conn:
            return _retry_transient(lambda: conn.execute(sql, params), sql).rowcount

    def executemany(self, sql: str, seq: Sequence[Sequence[Any]]) -> int:
        """批量写（自动提交），返回受影响行数。瞬时锁会退避重试。"""
        with self.connect() as conn:
            return _retry_transient(lambda: conn.executemany(sql, seq), sql).rowcount

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
    """把 schema 从 current 版本迁到 SCHEMA_VERSION。

    规则：**只追加、不改名不删列**。每加一版就在下面加一个 `if current < N` 分支，
    并递增 SCHEMA_VERSION。旧分支**永不删除**——别人的 .db 文件可能停在任意旧版本。

    注意新库（current=0）走的是完整 `_DDL`（它已经是最新结构），
    旧库才逐版本补 —— 两条路径最终必须得到同一结构，有测试锁这一点。
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

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def open_database(path: str | Path, *, timeout: float = 5.0) -> Database | None:
    """打开（必要时初始化）数据库；**任何失败都返回 None**。

    返回 None 表示持久化能力不可用，调用方应整体关闭该能力并让服务照常启动
    ——与本项目既有的「缺 paramiko → 关闭写命令能力」是同一套降级约定。
    """
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
