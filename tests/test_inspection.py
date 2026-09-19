"""统一指标目录驱动的巡检：阈值、等级、日志与调度。"""
from __future__ import annotations

import logging
import threading
import time

from src.core.inspection import (
    InspectionChecker,
    InspectionScheduler,
    MultiHostInspectionChecker,
    format_inspection_summary,
)
from src.interfaces.host import Host, HostRegistry, MonitorTarget
from src.interfaces.inspection import (
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_NORMAL,
    LEVEL_WARNING,
    InspectionFinding,
    InspectionResult,
    InspectionStore,
)
from src.interfaces.metric import (
    MetricRuntimeConfig,
    MetricSample,
    MetricSurfaceConfig,
    MetricsConfig,
)
from src.utils.config_loader import InspectionSettings
from tests.conftest import FakeMonitor


def _runtime(metric_id: str, warning=None, critical=None, severity=True, inspection=True):
    return MetricRuntimeConfig(
        metric_id=metric_id,
        enabled=True,
        surfaces=MetricSurfaceConfig(inspection=inspection),
        warning=warning,
        critical=critical,
        severity_enabled=severity,
    )


def _config(*runtime: MetricRuntimeConfig) -> MetricsConfig:
    return MetricsConfig(metrics={item.metric_id: item for item in runtime})


def _default_config() -> MetricsConfig:
    return _config(
        _runtime("cpu.usage", 80, 90),
        _runtime("memory.usage", 85, 95),
        _runtime("disk.usage", 80, 90),
        _runtime("alerts.active_count", 1, 3),
    )


def _settings() -> InspectionSettings:
    return InspectionSettings(enabled=True, interval_seconds=30, run_on_start=True)


def _monitor(**kwargs) -> FakeMonitor:
    kwargs.setdefault("cpu", 45.0)
    kwargs.setdefault("memory", 60.0)
    kwargs.setdefault("disks", [{"mountpoint": "/", "usage": 20.0}])
    kwargs.setdefault("alerts", [])
    return FakeMonitor(**kwargs)


def test_normal_round_contains_all_metric_snapshots():
    result = InspectionChecker(_monitor(), _settings(), _default_config()).run_once()

    assert result.level == LEVEL_NORMAL
    assert result.snapshot["cpu.usage"]["level"] == LEVEL_NORMAL
    assert result.snapshot["memory.usage"]["level"] == LEVEL_NORMAL
    assert result.snapshot["disk.usage"]["level"] == LEVEL_NORMAL
    assert result.snapshot["alerts.active_count"]["level"] == LEVEL_NORMAL
    assert result.findings == ()


def test_percentage_thresholds_are_inclusive():
    warning = InspectionChecker(
        _monitor(cpu=80.0), _settings(), _default_config()
    ).run_once()
    critical = InspectionChecker(
        _monitor(cpu=90.0), _settings(), _default_config()
    ).run_once()

    assert warning.level == LEVEL_WARNING
    assert warning.snapshot["cpu.usage"]["level"] == LEVEL_WARNING
    assert critical.level == LEVEL_CRITICAL
    assert critical.snapshot["cpu.usage"]["level"] == LEVEL_CRITICAL


def test_disk_uses_highest_sample_level():
    monitor = _monitor(
        disks=[
            {"mountpoint": "/", "usage": 50.0},
            {"mountpoint": "/data", "usage": 91.0},
        ]
    )

    result = InspectionChecker(monitor, _settings(), _default_config()).run_once()

    assert result.level == LEVEL_CRITICAL
    assert result.snapshot["disk.usage"]["level"] == LEVEL_CRITICAL
    assert any(f.metric == "disk.usage" and f.level == LEVEL_CRITICAL for f in result.findings)


def test_active_alert_count_maps_to_configured_levels():
    monitor = _monitor(
        alerts=[
            {"state": "firing", "alertname": "A"},
            {"state": "pending", "alertname": "B"},
        ]
    )

    result = InspectionChecker(monitor, _settings(), _default_config()).run_once()

    assert result.level == LEVEL_WARNING
    assert result.snapshot["alerts.active_count"]["count"] == 2
    assert result.snapshot["alerts.active_count"]["pending"] == 1
    assert result.snapshot["alerts.active_count"]["firing"] == 1


def test_observe_metric_does_not_change_overall_level():
    monitor = _monitor(metric_samples={"cpu.load1": [MetricSample("cpu.load1", {}, 99.0)]})
    config = _config(
        _runtime("cpu.load1", severity=False),
        _runtime("cpu.usage", 80, 90),
    )

    result = InspectionChecker(monitor, _settings(), config).run_once()

    assert result.level == LEVEL_NORMAL
    assert result.snapshot["cpu.load1"]["level"] == LEVEL_NORMAL
    assert result.snapshot["cpu.load1"]["samples"][0]["value"] == 99.0


def test_below_direction_uses_low_thresholds():
    monitor = _monitor(
        metric_samples={
            "memory.available": [MetricSample("memory.available", {}, 300.0)]
        }
    )
    config = _config(
        _runtime("memory.available", warning=1000, critical=500),
    )

    result = InspectionChecker(monitor, _settings(), config).run_once()

    assert result.level == LEVEL_CRITICAL


def test_single_query_failure_is_error_but_other_checks_continue():
    monitor = _monitor(raise_cpu=True)

    result = InspectionChecker(monitor, _settings(), _default_config()).run_once()

    assert result.level == LEVEL_ERROR
    assert result.snapshot["cpu.usage"]["level"] == LEVEL_ERROR
    assert result.snapshot["memory.usage"]["level"] == LEVEL_NORMAL
    assert "cpu.usage 查询失败" in result.error


def test_no_data_is_error():
    result = InspectionChecker(_monitor(cpu=None), _settings(), _default_config()).run_once()

    assert result.level == LEVEL_ERROR
    assert "cpu.usage 无有效数据" in result.error


def test_format_summary_contains_level_values_and_thresholds():
    result = InspectionChecker(_monitor(cpu=85.0), _settings(), _default_config()).run_once()

    text = format_inspection_summary(result)

    assert "[巡检] level=warning" in text
    assert "[CPU]" in text
    assert "cpu.usage [warning]" in text
    assert "85.0%" in text
    assert "warning>=80.0" in text
    assert "alerts.active_count [normal]: count=0" in text
    assert "[汇总]" in text


class RecordingStore(InspectionStore):
    def __init__(self) -> None:
        self.records: list[InspectionResult] = []

    def record(self, result: InspectionResult) -> None:
        self.records.append(result)

    def recent(self, *, limit: int = 50) -> list[InspectionResult]:
        return self.records[-limit:]

    def purge_expired(self) -> int:
        return 0


def test_scheduler_logs_and_stores_only_abnormal():
    class RecordingLogger:
        def __init__(self):
            self.records = []

        def log(self, level, message, *args):
            self.records.append((level, message % args if args else message))

        def error(self, message, *args, **kwargs):
            self.records.append((logging.ERROR, message % args if args else message))

    normal_logger = RecordingLogger()
    normal_store = RecordingStore()
    normal = InspectionScheduler(
        InspectionChecker(_monitor(), _settings(), _default_config()),
        _settings(),
        store=normal_store,
        logger_=normal_logger,
    )
    assert normal.run_once().level == LEVEL_NORMAL
    assert normal_store.records == []
    assert normal_logger.records[-1][0] == logging.INFO

    warning_logger = RecordingLogger()
    warning_store = RecordingStore()
    warning = InspectionScheduler(
        InspectionChecker(_monitor(cpu=85.0), _settings(), _default_config()),
        _settings(),
        store=warning_store,
        logger_=warning_logger,
    )
    assert warning.run_once().level == LEVEL_WARNING
    assert len(warning_store.records) == 1
    assert warning_logger.records[-1][0] == logging.WARNING


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

    scheduler = InspectionScheduler(Checker(), _settings())
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

    settings = InspectionSettings(enabled=True, interval_seconds=60, run_on_start=False)
    scheduler = InspectionScheduler(Checker(), settings)
    scheduler.start()
    time.sleep(0.05)
    scheduler.stop()
    assert count["n"] == 0


class TargetMonitor(FakeMonitor):
    def __init__(self, *, down=()):
        super().__init__(
            cpu=45.0,
            memory=60.0,
            disks=[{"mountpoint": "/", "usage": 20.0}],
            alerts=[],
        )
        self.down = set(down)
        self.seen = []

    def target_status(self, target):
        self.seen.append(("up", target.instance))
        return {
            "state": "unreachable" if target.instance in self.down else "reachable",
            "value": 0.0 if target.instance in self.down else 1.0,
        }

    def query_metric(self, metric_id, *, selector=None, target=None):
        self.seen.append((metric_id, target.instance))
        return super().query_metric(metric_id, selector=selector)

    def active_alerts(self, *, target=None):
        self.seen.append(("alerts", target.instance))
        return []


def _registry():
    return HostRegistry((
        Host("host-a", "A", True, MonitorTarget("node", "a:9100")),
        Host("host-b", "B", True, MonitorTarget("node", "b:9100")),
    ))


def test_multi_host_down_node_does_not_block_other_host():
    monitor = TargetMonitor(down={"a:9100"})
    checker = MultiHostInspectionChecker(
        monitor, _registry(), _settings(), _default_config()
    )

    results = checker.run_all()

    assert [(item.host_id, item.level) for item in results] == [
        ("host-a", LEVEL_ERROR),
        ("host-b", LEVEL_NORMAL),
    ]
    assert ("cpu.usage", "a:9100") not in monitor.seen
    assert ("cpu.usage", "b:9100") in monitor.seen


def test_multi_host_budget_rotates_fairly():
    settings = InspectionSettings(
        enabled=True,
        interval_seconds=30,
        max_hosts_per_run=1,
        max_queries_per_run=10,
    )
    checker = MultiHostInspectionChecker(
        TargetMonitor(), _registry(), settings, _default_config()
    )

    assert [item.host_id for item in checker.run_all()] == ["host-a"]
    assert [item.host_id for item in checker.run_all()] == ["host-b"]
