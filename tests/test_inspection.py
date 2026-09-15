"""固定指标巡检：阈值、等级、日志与调度。"""
from __future__ import annotations

import logging
import threading
import time

from src.core.inspection import (
    InspectionChecker,
    InspectionScheduler,
    format_inspection_summary,
)
from src.interfaces.inspection import (
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_NORMAL,
    LEVEL_WARNING,
    InspectionResult,
    InspectionStore,
)
from src.interfaces.monitor import MonitorError
from src.utils.config_loader import (
    ActiveAlertCheckSettings,
    InspectionChecksSettings,
    InspectionSettings,
    PercentageCheckSettings,
)
from tests.conftest import FakeMonitor


def _settings(**check_overrides) -> InspectionSettings:
    checks = InspectionChecksSettings(
        disk=PercentageCheckSettings(warning=80, critical=90),
        **check_overrides,
    )
    return InspectionSettings(
        enabled=True,
        interval_seconds=30,
        run_on_start=True,
        checks=checks,
    )


def _monitor(**kwargs) -> FakeMonitor:
    kwargs.setdefault("cpu", 45.0)
    kwargs.setdefault("memory", 60.0)
    kwargs.setdefault("disks", [{"mountpoint": "/", "usage": 20.0}])
    kwargs.setdefault("alerts", [])
    return FakeMonitor(**kwargs)


def test_normal_round_contains_all_metric_snapshots():
    result = InspectionChecker(_monitor(), _settings()).run_once()

    assert result.level == LEVEL_NORMAL
    assert result.snapshot["cpu"]["level"] == LEVEL_NORMAL
    assert result.snapshot["memory"]["level"] == LEVEL_NORMAL
    assert result.snapshot["disk"]["level"] == LEVEL_NORMAL
    assert result.snapshot["active_alerts"]["level"] == LEVEL_NORMAL
    assert result.findings == ()


def test_percentage_thresholds_are_inclusive():
    warning = InspectionChecker(_monitor(cpu=80.0), _settings()).run_once()
    critical = InspectionChecker(_monitor(cpu=90.0), _settings()).run_once()

    assert warning.level == LEVEL_WARNING
    assert warning.snapshot["cpu"]["level"] == LEVEL_WARNING
    assert critical.level == LEVEL_CRITICAL
    assert critical.snapshot["cpu"]["level"] == LEVEL_CRITICAL


def test_disk_uses_highest_mount_level():
    monitor = _monitor(
        disks=[
            {"mountpoint": "/", "usage": 50.0},
            {"mountpoint": "/data", "usage": 91.0},
        ]
    )

    result = InspectionChecker(monitor, _settings()).run_once()

    assert result.level == LEVEL_CRITICAL
    assert result.snapshot["disk"]["level"] == LEVEL_CRITICAL
    assert any(f.metric == "disk" and f.level == LEVEL_CRITICAL for f in result.findings)


def test_active_alert_count_maps_to_configured_levels():
    monitor = _monitor(
        alerts=[
            {"state": "firing", "alertname": "A"},
            {"state": "pending", "alertname": "B"},
        ]
    )
    settings = _settings(
        active_alerts=ActiveAlertCheckSettings(
            enabled=True, warning_count=1, critical_count=3
        )
    )

    result = InspectionChecker(monitor, settings).run_once()

    assert result.level == LEVEL_WARNING
    assert result.snapshot["active_alerts"]["count"] == 2
    assert result.snapshot["active_alerts"]["pending"] == 1
    assert result.snapshot["active_alerts"]["firing"] == 1


def test_single_query_failure_is_error_but_other_checks_continue():
    monitor = _monitor(raise_cpu=True)

    result = InspectionChecker(monitor, _settings()).run_once()

    assert result.level == LEVEL_ERROR
    assert result.snapshot["cpu"]["level"] == LEVEL_ERROR
    assert result.snapshot["memory"]["level"] == LEVEL_NORMAL
    assert "cpu 查询失败" in result.error


def test_no_data_is_error():
    result = InspectionChecker(_monitor(cpu=None), _settings()).run_once()

    assert result.level == LEVEL_ERROR
    assert "cpu 无有效数据" in result.error


def test_format_summary_contains_level_values_and_thresholds():
    result = InspectionChecker(_monitor(cpu=85.0), _settings()).run_once()

    text = format_inspection_summary(result)

    assert "level=warning" in text
    assert "cpu=85.0%" in text
    assert "warn>=80.0" in text
    assert "active_alerts=0" in text


class RecordingStore(InspectionStore):
    def __init__(self) -> None:
        self.records: list[InspectionResult] = []

    def record(self, result: InspectionResult) -> None:
        self.records.append(result)

    def recent(self, *, limit: int = 50) -> list[InspectionResult]:
        return self.records[-limit:]

    def purge_expired(self) -> int:
        return 0


def test_scheduler_logs_and_stores_only_abnormal(caplog):
    normal_store = RecordingStore()
    normal = InspectionScheduler(
        InspectionChecker(_monitor(), _settings()),
        _settings(),
        store=normal_store,
    )
    with caplog.at_level(logging.INFO, logger="ops_agent.inspection"):
        assert normal.run_once().level == LEVEL_NORMAL
    assert normal_store.records == []
    assert caplog.records[-1].levelno == logging.INFO

    warning_store = RecordingStore()
    warning = InspectionScheduler(
        InspectionChecker(_monitor(cpu=85.0), _settings()),
        _settings(),
        store=warning_store,
    )
    with caplog.at_level(logging.INFO, logger="ops_agent.inspection"):
        assert warning.run_once().level == LEVEL_WARNING
    assert len(warning_store.records) == 1
    assert caplog.records[-1].levelno == logging.WARNING


def test_scheduler_turns_checker_exception_into_error_result():
    class BoomChecker:
        def run_once(self):
            raise RuntimeError("checker crashed")

    store = RecordingStore()
    scheduler = InspectionScheduler(BoomChecker(), _settings(), store=store)

    result = scheduler.run_once()

    assert result.level == LEVEL_ERROR
    assert "checker crashed" in result.error
    assert len(store.records) == 1


def test_scheduler_run_on_start_and_stop():
    calls = threading.Event()
    count = {"n": 0}

    class Checker:
        def run_once(self):
            count["n"] += 1
            calls.set()
            return InspectionResult(checked_at="t", level=LEVEL_NORMAL)

    settings = InspectionSettings(
        enabled=True,
        interval_seconds=60,
        run_on_start=True,
        checks=InspectionChecksSettings(
            cpu=PercentageCheckSettings(enabled=True),
            memory=PercentageCheckSettings(enabled=False),
            disk=PercentageCheckSettings(enabled=False),
            active_alerts=ActiveAlertCheckSettings(enabled=False),
        ),
    )
    scheduler = InspectionScheduler(Checker(), settings)
    scheduler.start()
    try:
        assert calls.wait(timeout=1)
        assert scheduler.running is True
    finally:
        scheduler.stop()
    assert scheduler.running is False
    assert count["n"] == 1


def test_scheduler_run_on_start_false_waits_for_interval():
    count = {"n": 0}

    class Checker:
        def run_once(self):
            count["n"] += 1
            return InspectionResult(checked_at="t", level=LEVEL_NORMAL)

    settings = InspectionSettings(
        enabled=True,
        interval_seconds=60,
        run_on_start=False,
        checks=InspectionChecksSettings(
            cpu=PercentageCheckSettings(enabled=True),
            memory=PercentageCheckSettings(enabled=False),
            disk=PercentageCheckSettings(enabled=False),
            active_alerts=ActiveAlertCheckSettings(enabled=False),
        ),
    )
    scheduler = InspectionScheduler(Checker(), settings)
    scheduler.start()
    time.sleep(0.05)
    scheduler.stop()
    assert count["n"] == 0
