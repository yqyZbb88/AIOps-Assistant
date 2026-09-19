"""异常巡检 SQLite 归档：只存异常、排序、清理与降级。"""
from __future__ import annotations

import sqlite3

from src.interfaces.inspection import (
    LEVEL_CRITICAL,
    LEVEL_NORMAL,
    LEVEL_WARNING,
    InspectionError,
    InspectionFinding,
    InspectionResult,
)
from src.storage.db import open_database
from src.storage.inspection import SqliteInspectionStore


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _result(
    level=LEVEL_WARNING,
    checked_at="2026-01-01T00:00:00+00:00",
    host_id="",
):
    return InspectionResult(
        checked_at=checked_at,
        level=level,
        host_id=host_id,
        snapshot={"cpu": {"value": 85.0}},
        findings=(InspectionFinding("cpu", level, "cpu high", value=85.0, threshold=80),),
    )


def _store(tmp_path, clock=None, max_rows=100):
    db = open_database(tmp_path / "ops.db")
    assert db is not None
    return SqliteInspectionStore(
        db,
        ttl_seconds=3600,
        max_rows=max_rows,
        clock=clock or FakeClock(),
    )


def test_normal_result_is_not_stored(tmp_path):
    store = _store(tmp_path)

    store.record(_result(level=LEVEL_NORMAL))

    assert store.recent() == []


def test_abnormal_result_round_trips(tmp_path):
    store = _store(tmp_path)

    store.record(_result())

    rows = store.recent()
    assert len(rows) == 1
    assert rows[0].record_id is not None
    assert rows[0].level == LEVEL_WARNING
    assert rows[0].snapshot["cpu"]["value"] == 85.0
    assert rows[0].findings[0].message == "cpu high"


def test_recent_is_newest_first(tmp_path):
    store = _store(tmp_path)
    store.record(_result(checked_at="2026-01-01T00:00:00+00:00"))
    store.record(_result(LEVEL_CRITICAL, checked_at="2026-01-01T00:00:01+00:00"))

    assert [r.checked_at for r in store.recent()] == [
        "2026-01-01T00:00:01+00:00",
        "2026-01-01T00:00:00+00:00",
    ]


def test_recent_can_be_filtered_by_host(tmp_path):
    store = _store(tmp_path)
    store.record(_result(host_id="host-a"))
    store.record(
        _result(
            LEVEL_CRITICAL,
            checked_at="2026-01-01T00:00:01+00:00",
            host_id="host-b",
        )
    )

    rows = store.recent(host_id="host-a")
    assert len(rows) == 1
    assert rows[0].host_id == "host-a"


def test_purge_by_ttl(tmp_path):
    clock = FakeClock()
    store = _store(tmp_path, clock=clock)
    # 直接写库，避免 record() 的顺带清理提前删掉待测试记录。
    for checked_at, value in (
        ("2020-01-01T00:00:00+00:00", "old"),
        ("2026-01-01T00:00:00+00:00", "new"),
    ):
        store.db.execute(
            "INSERT INTO inspection_runs "
            "(checked_at, level, snapshot_json, findings_json, error_text) "
            "VALUES (?, ?, ?, ?, '')",
            (checked_at, LEVEL_WARNING, '{}', '[]'),
        )

    removed = store.purge_expired()

    assert removed == 1
    assert [r.checked_at for r in store.recent()] == ["2026-01-01T00:00:00+00:00"]


def test_purge_by_capacity_keeps_newest(tmp_path):
    store = _store(tmp_path, max_rows=2)
    for second in range(3):
        store.record(_result(checked_at=f"2026-01-01T00:00:0{second}+00:00"))

    rows = store.recent()
    assert len(rows) == 2
    assert [r.checked_at for r in rows] == [
        "2026-01-01T00:00:02+00:00",
        "2026-01-01T00:00:01+00:00",
    ]


def test_storage_failure_marks_unhealthy(tmp_path):
    store = _store(tmp_path)
    store.db.execute = lambda *a, **k: (_ for _ in ()).throw(
        sqlite3.OperationalError("disk full")
    )

    try:
        store.record(_result())
    except InspectionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("应抛 InspectionError")

    assert store.healthy is False
    assert store.last_error
