"""自动处置幂等守卫：内存、SQLite、并发与 fail-closed。"""
from __future__ import annotations

import sqlite3
import threading

from src.storage.db import open_database
from src.storage.remediation_guard import (
    InMemoryRuleExecutionGuard,
    SqliteRuleExecutionGuard,
)


def _claim(guard, suffix="a") -> bool:
    return guard.claim(
        alert_key=f"fp-{suffix}",
        action="restart_service",
        alert_status="firing",
        starts_at="2026-01-01T00:00:00Z",
    )


def test_memory_guard_claims_once():
    guard = InMemoryRuleExecutionGuard()
    assert _claim(guard) is True
    assert _claim(guard) is False


def test_memory_guard_is_atomic_under_concurrency():
    guard = InMemoryRuleExecutionGuard()
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(_claim(guard))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert results.count(True) == 1
    assert results.count(False) == 7


def test_sqlite_guard_persists_claims(tmp_path):
    db = open_database(tmp_path / "ops.db")
    assert db is not None
    first = SqliteRuleExecutionGuard(db)
    second = SqliteRuleExecutionGuard(open_database(tmp_path / "ops.db"))

    assert _claim(first) is True
    assert _claim(second) is False


def test_sqlite_guard_fails_closed_on_storage_error(tmp_path):
    db = open_database(tmp_path / "ops.db")
    assert db is not None
    guard = SqliteRuleExecutionGuard(db)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    db.execute = boom  # type: ignore[method-assign]
    assert _claim(guard) is False
