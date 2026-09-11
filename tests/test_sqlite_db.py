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


# -- 迁移（v1 → v2） --------------------------------------------------------
def _downgrade_to_v1(path) -> None:
    """把一个 v2 库改造成 v1 的样子：去掉 name 列与它的索引，user_version 改回 1。

    v2 的 sessions 就是 v1 的 sessions 加一列，所以「v2 减去 name」恰好等价于 v1 结构。
    """
    with sqlite3.connect(str(path)) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_sessions_user_name")
        conn.execute("ALTER TABLE sessions DROP COLUMN name")
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


def test_migration_can_be_named_again(tmp_path):
    """升级后唯一索引要真的生效——迁移里漏建索引的话这里会挂。"""
    path = tmp_path / "ops.db"
    open_database(path)
    _downgrade_to_v1(path)
    db = open_database(path)

    db.execute(
        "INSERT INTO sessions (session_id, user, name, created_at, updated_at) VALUES (?,?,?,?,?)",
        ("s1", "ops", "dup", "t", "t"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO sessions (session_id, user, name, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("s2", "ops", "dup", "t", "t"),
        )


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
