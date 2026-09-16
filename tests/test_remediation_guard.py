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


class FakeClock:
    """可控时钟：离线验证保留期，不依赖真实等待（沿用 test_session_store 的范式）。"""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_memory_guard_claims_once():
    guard = InMemoryRuleExecutionGuard()
    assert _claim(guard) is True
    assert _claim(guard) is False


def test_memory_guard_expires_claims_after_ttl():
    """保留期与 SQLite 版同源（默认 history_ttl_hours = 7 天）。

    内存版若永不落定过期，`history_ttl_hours` 就在**降级路径**上静默失效了——
    而降级不该悄悄改变一个安全相关的窗口。
    """
    clock = FakeClock()
    guard = InMemoryRuleExecutionGuard(ttl_seconds=100, clock=clock)
    assert _claim(guard) is True
    assert _claim(guard) is False

    clock.advance(101)

    assert _claim(guard) is True, "过了保留期，同一条告警的同一个动作可以重新执行"


def test_memory_guard_keeps_claims_inside_ttl():
    """没过期的抢占必须照拦——清过期项不能连新鲜的也一起丢掉。"""
    clock = FakeClock()
    guard = InMemoryRuleExecutionGuard(ttl_seconds=100, clock=clock)
    assert _claim(guard, "old") is True
    clock.advance(99)
    assert _claim(guard, "fresh") is True
    clock.advance(1)  # old 满 100s 过期，fresh 才 1s
    assert _claim(guard, "fresh") is False, "fresh 还在保留期内"
    assert _claim(guard, "old") is True, "old 刚好到期"


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
