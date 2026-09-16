"""SQLite 存储底座：建表、版本双向校验、降级、连接关闭（全部离线，用 tmp_path）。"""
from __future__ import annotations

import os
import sqlite3

import pytest

from src.storage.db import SCHEMA_VERSION, Database, open_database

_EXPECTED_TABLES = {
    "sessions",
    "messages",
    "alerts",
    "remediations",
    "ai_advice",
    "approval_decisions",
    "remediation_claims",
    "inspection_runs",
}


def _tables(db_path) -> set[str]:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _user_version(db_path) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])


# -- 建表 -------------------------------------------------------------------
def test_open_creates_all_tables(tmp_path):
    db = open_database(tmp_path / "ops.db")

    assert db is not None
    assert _EXPECTED_TABLES <= _tables(db.path)


def test_open_writes_schema_version(tmp_path):
    db = open_database(tmp_path / "ops.db")
    assert _user_version(db.path) == SCHEMA_VERSION


def test_open_is_idempotent(tmp_path):
    """重复打开（服务重启 / 多组件共享）不得报错，也不得清空已有数据。"""
    path = tmp_path / "ops.db"
    first = open_database(path)
    first.execute("INSERT INTO sessions (session_id, created_at, updated_at) VALUES (?,?,?)",
                  ("s1", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))

    second = open_database(path)

    assert second is not None
    assert second.query("SELECT session_id FROM sessions") == [{"session_id": "s1"}]


def test_sessions_columns_are_exact(tmp_path):
    """列名写错会让后续 INSERT 报 no such column 再被上层吞掉——这里显式锁住。

    注意**没有** pending_approvals 列：待审批数由 messages 状态实时统计得出。
    存成列就得在 append_turn / update_pending / reconcile_pending 三处同步维护，
    批量作废时极易漂移；实时统计则不可能算错。

    `name` 是 v2 加的会话别名列（客户端可读；session_id 仍是服务端随机值）。
    """
    db = open_database(tmp_path / "ops.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(sessions)")}
    assert cols == {
        "session_id", "user", "name", "title", "status", "created_at", "updated_at"
    }


def test_approval_decisions_columns_are_exact(tmp_path):
    """这张表的列由迁移测试锁定，改列必须同步迁移和测试。"""
    db = open_database(tmp_path / "ops.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(approval_decisions)")}
    assert cols == {"approval_id", "session_id", "action", "decided_at", "decided_by"}


def test_remediation_claims_columns_are_exact(tmp_path):
    db = open_database(tmp_path / "ops.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(remediation_claims)")}
    assert cols == {"alert_key", "action", "alert_status", "starts_at", "claimed_at"}


def test_v4_db_gains_remediation_claims(tmp_path):
    """v4 → v5：旧库升级后必须有幂等抢占表。"""
    path = tmp_path / "ops.db"
    db = open_database(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("DROP TABLE remediation_claims")
        conn.execute("PRAGMA user_version = 4")

    migrated = open_database(path)

    assert migrated is not None
    assert _user_version(path) == SCHEMA_VERSION
    assert "remediation_claims" in _tables(path)


def test_inspection_runs_columns_are_exact(tmp_path):
    db = open_database(tmp_path / "ops.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(inspection_runs)")}
    assert cols == {
        "id", "checked_at", "level", "snapshot_json", "findings_json", "error_text"
    }


def test_v5_db_gains_inspection_runs(tmp_path):
    """v5 → v6：旧库升级后必须有异常巡检归档表。"""
    path = tmp_path / "ops.db"
    open_database(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("DROP TABLE inspection_runs")
        conn.execute("PRAGMA user_version = 5")

    migrated = open_database(path)

    assert migrated is not None
    assert _user_version(path) == SCHEMA_VERSION
    assert "inspection_runs" in _tables(path)


def test_v3_db_gains_decided_by(tmp_path):
    """v3 → v4：老的决策记录拿到默认空串，不丢数据。

    （`decided_by` 为什么必须落库而不只是写审计日志：日志会轮转被冲掉，
    而这张表的 TTL 是 7 天——回头追问「那条 unknown 是谁批的」时日志早没了。）
    """
    path = tmp_path / "ops.db"
    db = open_database(path)
    db.execute(
        "INSERT INTO approval_decisions (approval_id, session_id, action, decided_at) "
        "VALUES (?,?,?,?)",
        ("ap-old", "s1", "approve", "2026-01-01T00:00:00+00:00"),
    )
    with sqlite3.connect(str(path)) as conn:  # 手工退回 v3 的样子
        conn.execute("ALTER TABLE approval_decisions DROP COLUMN decided_by")
        conn.execute("PRAGMA user_version = 3")

    migrated = open_database(path)

    assert migrated is not None
    assert _user_version(path) == SCHEMA_VERSION
    assert migrated.query("SELECT approval_id, decided_by FROM approval_decisions") == [
        {"approval_id": "ap-old", "decided_by": ""}
    ], "老记录保住，新列给默认值"


# -- 迁移（v1 → v2） --------------------------------------------------------
def _downgrade_to_v1(path) -> None:
    """把一个**当前版本**的库改造成 v1 的样子，用于验证逐级迁移。

    **每个后续版本加的列都要在这里撤掉**——否则那个列在「假装成 v1」的库里其实还在，
    升到对应那一步的 `ALTER TABLE ADD COLUMN` 会撞 `duplicate column name`，
    于是迁移失败、`open_database` 返回 None，报错还容易让人以为是迁移写错了。
    （v2 加 sessions.name、v4 加 approval_decisions.decided_by。）
    """
    with sqlite3.connect(str(path)) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_sessions_user_name")
        conn.execute("ALTER TABLE sessions DROP COLUMN name")
        conn.execute("ALTER TABLE approval_decisions DROP COLUMN decided_by")
        conn.execute("PRAGMA user_version = 1")


def test_v1_file_is_migrated_to_v2(tmp_path):
    """旧库文件必须能就地升级——**且原有数据一条不丢**。"""
    path = tmp_path / "ops.db"
    db = open_database(path)
    db.execute(
        "INSERT INTO sessions (session_id, user, name, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("s1", "ops", "old_name", "t", "t"),
    )
    _downgrade_to_v1(path)

    migrated = open_database(path)

    assert migrated is not None
    assert _user_version(path) == SCHEMA_VERSION
    rows = migrated.query("SELECT session_id, user, name FROM sessions")
    assert rows == [{"session_id": "s1", "user": "ops", "name": ""}], "数据要保住，新列给默认值"


def _structure(path):
    """库的结构指纹：表名、每表**有序**的列定义、索引名。

    刻意**不比 `sqlite_master.sql` 的文本**：ALTER TABLE 之后 SQLite 会用自己的
    规范格式重写那段 DDL（缩进、换行全变），文本相同是不可能的。要比的是结构。
    """
    with sqlite3.connect(str(path)) as conn:
        tables = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        columns = {
            t: [
                (r[1], r[2], r[3], r[4])  # 名、类型、notnull、默认值（顺序即 cid 顺序）
                for r in conn.execute(f"PRAGMA table_info({t})")
            ]
            for t in tables
        }
        indexes = sorted(
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_autoindex_%'"
            )
        )
    return tables, columns, indexes


def test_migrated_db_matches_fresh_db(tmp_path):
    """迁移出来的结构与全新建库**必须一致**——两条路径分叉是迁移最经典的翻车方式。

    比结构而非文本；**列顺序也在比较范围内**（迁移用 ALTER 只能追加到末尾，
    所以全新建库的 DDL 也把新列写在末尾，两者列序才对得上）。
    """
    fresh_path, migrated_path = tmp_path / "fresh.db", tmp_path / "old.db"
    open_database(fresh_path)

    open_database(migrated_path)
    _downgrade_to_v1(migrated_path)
    open_database(migrated_path)

    assert _structure(fresh_path) == _structure(migrated_path)


def _name_index_names(path) -> set[str]:
    with sqlite3.connect(str(path)) as conn:
        return {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }


def test_v3_allows_duplicate_session_names(tmp_path):
    """v3 撤掉了 (user, name) 唯一索引——名字降级为纯标签，允许重名。"""
    path = tmp_path / "ops.db"
    db = open_database(path)

    sql = "INSERT INTO sessions (session_id, user, name, created_at, updated_at) VALUES (?,?,?,?,?)"
    db.execute(sql, ("s1", "ops", "dup", "t", "t"))
    db.execute(sql, ("s2", "ops", "dup", "t", "t"))  # 不该再冲突

    assert len(db.query("SELECT 1 FROM sessions")) == 2


def test_v1_migration_ends_without_the_unique_index(tmp_path):
    """从 v1 一路升到最新：中间那个 v2 分支建的索引，必须被 v3 分支撤掉。

    这条专测「每个历史分支都按序执行」——若 v1→v3 时跳过了 v2 分支，
    索引压根不会存在（测不出来）；若执行了 v2 却漏了 v3，这里就会挂。
    """
    path = tmp_path / "ops.db"
    open_database(path)
    _downgrade_to_v1(path)
    open_database(path)  # v1 → v3

    assert "idx_sessions_user_name" not in _name_index_names(path)


def test_v2_migration_drops_the_index(tmp_path):
    """v2 → v3：一个**带**唯一索引的 v2 库，升上来之后索引要被撤掉。"""
    path = tmp_path / "ops.db"
    open_database(path)
    _downgrade_to_v1(path)
    # 手工造一个 v2 状态的库：加回名字列与那个索引，版本号写 2
    with sqlite3.connect(str(path)) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN name TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE UNIQUE INDEX idx_sessions_user_name ON sessions (user, name) "
            "WHERE name <> ''"
        )
        conn.execute("PRAGMA user_version = 2")
    assert "idx_sessions_user_name" in _name_index_names(path)

    db = open_database(path)

    assert _user_version(path) == SCHEMA_VERSION
    assert "idx_sessions_user_name" not in _name_index_names(path)
    sql = "INSERT INTO sessions (session_id, user, name, created_at, updated_at) VALUES (?,?,?,?,?)"
    db.execute(sql, ("s1", "ops", "dup", "t", "t"))
    db.execute(sql, ("s2", "ops", "dup", "t", "t"))  # 重名不再冲突


# -- 版本双向校验 -----------------------------------------------------------
def test_version_newer_than_code_closes_persistence(tmp_path):
    """文件来自更新版本的代码 → 拒绝使用（绝不写入，以免写坏对方数据）。"""
    path = tmp_path / "ops.db"
    open_database(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    assert open_database(path) is None


def test_version_newer_does_not_write(tmp_path):
    """拒绝之后不得留下任何新写入（哪怕只写了个表）。"""
    path = tmp_path / "ops.db"
    open_database(path)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")

    open_database(path)

    assert _user_version(path) == SCHEMA_VERSION + 5, "不得回写版本号"


# -- 降级 -------------------------------------------------------------------
def test_open_on_unusable_path_returns_none(tmp_path):
    """父路径被一个普通文件占住 → mkdir 失败 → 降级为 None（服务照常启动）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    assert open_database(blocker / "sub" / "ops.db") is None


def test_fresh_path_creates_parent_dirs(tmp_path):
    """data/ 这类目录默认不存在，必须自动创建。"""
    db = open_database(tmp_path / "nested" / "deeper" / "ops.db")
    assert db is not None and db.path.is_file()


# -- 连接 / 事务 -------------------------------------------------------------
def test_journal_mode_reported(tmp_path):
    """本机 tmp_path 是本地盘，WAL 应能开起来；同步盘会回落 delete 且不影响可用性。"""
    db = open_database(tmp_path / "ops.db")
    assert db.journal_mode in {"wal", "delete"}


def test_connection_is_closed_after_use(tmp_path):
    """漏关连接的实测表现：Windows 上文件删不掉。这里用它当探针。"""
    path = tmp_path / "ops.db"
    db = open_database(path)
    db.query("SELECT 1")
    db.execute("INSERT INTO sessions (session_id, created_at, updated_at) VALUES (?,?,?)",
               ("s1", "t", "t"))

    os.remove(path)  # 句柄未关闭则抛 PermissionError

    assert not path.exists()


def test_query_and_execute_roundtrip(tmp_path):
    db = open_database(tmp_path / "ops.db")
    db.execute("INSERT INTO ai_advice (alert_key, alertname, advice, analyzed_at) VALUES (?,?,?,?)",
               ("k1", "HighCPU", "先看 top", "2026-01-01T00:00:00+00:00"))

    rows = db.query("SELECT alertname, advice FROM ai_advice WHERE alert_key = ?", ("k1",))

    assert rows == [{"alertname": "HighCPU", "advice": "先看 top"}]


def test_transaction_commits(tmp_path):
    db = open_database(tmp_path / "ops.db")
    with db.transaction() as conn:
        conn.execute("INSERT INTO sessions (session_id, created_at, updated_at) VALUES (?,?,?)",
                     ("s1", "t", "t"))

    assert len(db.query("SELECT 1 FROM sessions")) == 1


def test_transaction_rolls_back_on_error(tmp_path):
    """同轮两行必须同事务——半批落库正是要避免的状态。"""
    db = open_database(tmp_path / "ops.db")
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO sessions (session_id, created_at, updated_at) VALUES (?,?,?)",
                         ("s1", "t", "t"))
            raise RuntimeError("模拟第二轮写入前崩溃")

    assert db.query("SELECT 1 FROM sessions") == []


def test_unique_constraint_is_enforced(tmp_path):
    """messages 的 UNIQUE(session_id, seq) 是 seq 递增逻辑的最后一道防线。"""
    db = open_database(tmp_path / "ops.db")
    sql = "INSERT INTO messages (session_id, seq, role, content, created_at) VALUES (?,?,?,?,?)"
    db.execute(sql, ("s1", 1, "user", "hi", "t"))

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, ("s1", 1, "assistant", "dup", "t"))


def test_database_repr_mentions_path(tmp_path):
    db = Database(tmp_path / "ops.db")
    assert "ops.db" in repr(db)


# -- 瞬时锁退避重试 ----------------------------------------------------------
@pytest.fixture
def no_retry_delay(monkeypatch):
    """把退避间隔归零，让重试测试不必真的等 0.7 秒。"""
    import src.storage.db as dbmod

    monkeypatch.setattr(dbmod, "_TRANSIENT_RETRY_DELAYS", (0.0, 0.0))


def test_is_transient_classifies_errors():
    from src.storage.db import _is_transient

    assert _is_transient(sqlite3.OperationalError("database is locked"))
    assert _is_transient(sqlite3.OperationalError("database table is busy"))
    # 这些重试没有意义，必须立即抛出
    assert not _is_transient(sqlite3.OperationalError("no such table: nope"))
    assert not _is_transient(sqlite3.IntegrityError("UNIQUE constraint failed"))
    assert not _is_transient(RuntimeError("database is locked"))  # 非 sqlite 异常


def test_retry_transient_recovers(no_retry_delay):
    """前两次撞锁、第三次成功 → 整体成功。这正是「瞬时争用」的形态。"""
    from src.storage.db import _retry_transient

    calls = []

    def op():
        calls.append(1)
        if len(calls) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert _retry_transient(op, "INSERT INTO x") == "ok"
    assert len(calls) == 3


def test_retry_transient_gives_up_after_all_attempts(no_retry_delay):
    from src.storage.db import _retry_transient

    calls = []

    def op():
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        _retry_transient(op, "INSERT INTO x")
    assert len(calls) == 3, "0.0 + 两个退避间隔 = 共 3 次尝试"


def test_retry_transient_does_not_retry_permanent_errors(no_retry_delay):
    from src.storage.db import _retry_transient

    calls = []

    def op():
        calls.append(1)
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError):
        _retry_transient(op, "INSERT INTO x")
    assert len(calls) == 1, "非瞬时错误重试没有意义，必须只试一次"


def test_execute_retries_transient_lock(tmp_path, no_retry_delay, monkeypatch):
    """端到端：`Database.execute` 撞一次瞬时锁之后自己恢复，调用方无感。"""
    db = open_database(tmp_path / "ops.db")
    real_connect = db.connect
    state = {"n": 1}

    class Flaky:
        """第一次 BEGIN/INSERT 抛 locked，之后放行。"""
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if state["n"] > 0 and sql.lstrip().upper().startswith("INSERT"):
                state["n"] -= 1
                raise sqlite3.OperationalError("database is locked")
            return self._conn.execute(sql, params)

    import contextlib

    @contextlib.contextmanager
    def patched():
        with real_connect() as conn:
            yield Flaky(conn)

    monkeypatch.setattr(db, "connect", patched)

    db.execute(
        "INSERT INTO sessions (session_id, created_at, updated_at) VALUES (?,?,?)",
        ("s1", "t", "t"),
    )

    assert len(db.query("SELECT 1 FROM sessions")) == 1
