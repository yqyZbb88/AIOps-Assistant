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


def test_sessions_has_pending_approvals_column(tmp_path):
    """列名写错会让后续 INSERT 报 no such column 再被上层吞掉——这里显式锁住。"""
    db = open_database(tmp_path / "ops.db")
    cols = {r["name"] for r in db.query("PRAGMA table_info(sessions)")}
    assert {"session_id", "user", "title", "status", "created_at", "updated_at",
            "pending_approvals"} <= cols


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
