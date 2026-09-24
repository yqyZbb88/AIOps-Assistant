"""统一指标目录驱动的固定指标巡检。"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from src.interfaces.inspection import (
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_NORMAL,
    LEVEL_WARNING,
    LEVEL_ORDER,
    InspectionFinding,
    InspectionResult,
    InspectionStore,
    max_level,
)
from src.interfaces.host import Host, HostRegistry
from src.interfaces.metric import MetricDefinition, MetricRuntimeConfig, MetricsConfig
from src.interfaces.monitor import MonitorSource
from src.plugins.monitors.prometheus_catalog import METRIC_CATALOG
from src.utils.config_loader import InspectionSettings
from src.utils.logger import get_inspection_logger

logger = logging.getLogger("aiops_assistant.inspection.runner")

_LEGACY_LABELS = {
    "cpu.usage": "cpu",
    "memory.usage": "memory",
    "disk.usage": "disk",
    "alerts.active_count": "active_alerts",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt_value(value: Any, unit: str) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "bytes":
        return f"{value:.0f}B"
    if unit == "MiB/s":
        return f"{value:.1f}MiB/s"
    if unit in {"errors/s", "load/core"}:
        return f"{value:.3f}{unit}"
    if unit == "alerts":
        return f"{value:g} alerts"
    return f"{value:g}{unit}"


def _threshold_level(
    value: float,
    runtime: MetricRuntimeConfig,
    definition: MetricDefinition,
) -> str:
    if not runtime.severity_enabled or definition.direction == "observe":
        return LEVEL_NORMAL
    warning = runtime.warning
    critical = runtime.critical
    if warning is None or critical is None:
        return LEVEL_NORMAL
    if definition.direction == "above":
        if value >= critical:
            return LEVEL_CRITICAL
        if value >= warning:
            return LEVEL_WARNING
    elif definition.direction == "below":
        if value <= critical:
            return LEVEL_CRITICAL
        if value <= warning:
            return LEVEL_WARNING
    return LEVEL_NORMAL


def format_inspection_summary(result: InspectionResult) -> str:
    """把一轮结果渲染成适合人工阅读的多行日志。"""
    host_text = f" host_id={result.host_id}" if result.host_id else ""
    lines = [
        f"[巡检]{host_text} level={result.level} checked_at={result.checked_at}"
    ]
    current_group: str | None = None

    for metric_id, item in result.snapshot.items():
        if not item.get("enabled"):
            continue
        group = str(item.get("group") or "Other")
        if group != current_group:
            lines.append(f"[{group}]")
            current_group = group

        unit = str(item.get("unit") or "")
        thresholds = _threshold_text(item)
        if metric_id == "alerts.active_count":
            detail = (
                f"count={item.get('count', 0)} "
                f"pending={item.get('pending', 0)} firing={item.get('firing', 0)}"
            )
            suffix = f" ({thresholds})" if thresholds else ""
            lines.append(
                f"  {metric_id} [{item.get('level')}]: {detail}{suffix}"
            )
            continue

        samples = item.get("samples") or []
        if not samples:
            lines.append(f"  {metric_id} [{item.get('level')}]: 无数据")
            continue
        for sample in samples:
            label = _sample_label(sample)
            value = _fmt_value(sample.get("value"), unit)
            suffix = f" ({thresholds})" if thresholds else ""
            lines.append(
                f"  {metric_id} [{sample.get('level')}] {label}: {value}{suffix}"
            )

    lines.append("[汇总]")
    lines.append(
        f"  overall={result.level} metrics={len(result.snapshot)} anomalies={len(result.findings)}"
    )
    if result.error:
        lines.append(f"  errors={result.error}")
    return "\n".join(lines)


def _threshold_text(item: Mapping[str, Any]) -> str:
    if not item.get("severity_enabled"):
        return ""
    warning = item.get("warning")
    critical = item.get("critical")
    if warning is None or critical is None:
        return ""
    unit = str(item.get("unit") or "")
    op = "<=" if item.get("direction") == "below" else ">="
    return (
        f"warning{op}{_fmt_value(warning, unit)}, "
        f"critical{op}{_fmt_value(critical, unit)}"
    )




def _sample_label(sample: Mapping[str, Any]) -> str:
    labels = sample.get("labels") or {}
    if not labels:
        return "value"
    return ",".join(f"{key}={value}" for key, value in labels.items())


class InspectionChecker:
    """按 metrics.yaml 顺序执行所有 inspection surface 指标。"""

    def __init__(
        self,
        monitor: MonitorSource,
        settings: InspectionSettings,
        metrics_config: MetricsConfig,
        *,
        catalog: tuple[MetricDefinition, ...] = METRIC_CATALOG,
        clock: Callable[[], str] | None = None,
        host: Host | None = None,
    ) -> None:
        self.monitor = monitor
        self.settings = settings
        self.metrics_config = metrics_config
        self.catalog = {definition.metric_id: definition for definition in catalog}
        self._clock = clock or _now_iso
        self.host = host

    def run_once(self) -> InspectionResult:
        checked_at = self._clock()
        snapshot: dict[str, Any] = {}
        findings: list[InspectionFinding] = []
        errors: list[str] = []
        levels: list[str] = []

        if self.host is not None:
            try:
                exporter = self.monitor.target_status(self.host.monitor)
            except Exception as exc:
                exporter = {"state": "query_error", "value": None}
                errors.append(f"Exporter 状态查询失败: {exc}")
            state = str(exporter.get("state") or "query_error")
            snapshot["exporter.up"] = {
                "enabled": True,
                "display_name": "Exporter 状态",
                "group": "System",
                "unit": "",
                "level": LEVEL_NORMAL if state == "reachable" else LEVEL_ERROR,
                "state": state,
                "value": exporter.get("value"),
                "samples": [],
            }
            if state != "reachable":
                message = (
                    f"Exporter 状态为 {state}；这只表示采集目标状态，"
                    "不能据此断言主机关机"
                )
                findings.append(
                    InspectionFinding("exporter.up", LEVEL_ERROR, message)
                )
                if not errors:
                    errors.append(message)
                return InspectionResult(
                    checked_at=checked_at,
                    level=LEVEL_ERROR,
                    host_id=self.host.host_id,
                    snapshot=snapshot,
                    findings=tuple(findings),
                    error="; ".join(errors),
                )

        for runtime in self.metrics_config.selected("inspection"):
            definition = self.catalog.get(runtime.metric_id)
            if definition is None:
                message = f"巡检指标未注册: {runtime.metric_id}"
                errors.append(message)
                findings.append(InspectionFinding(runtime.metric_id, LEVEL_ERROR, message))
                levels.append(LEVEL_ERROR)
                continue
            if definition.kind == "alert_count":
                levels.append(
                    self._check_active_alerts(definition, runtime, snapshot, findings, errors)
                )
            else:
                levels.append(
                    self._check_metric(definition, runtime, snapshot, findings, errors)
                )

        overall = max_level(*levels, LEVEL_ERROR if errors else LEVEL_NORMAL)
        return InspectionResult(
            checked_at=checked_at,
            level=overall,
            host_id=self.host.host_id if self.host is not None else "",
            snapshot=snapshot,
            findings=tuple(findings),
            error="; ".join(errors),
        )

    def _check_metric(
        self,
        definition: MetricDefinition,
        runtime: MetricRuntimeConfig,
        snapshot: dict[str, Any],
        findings: list[InspectionFinding],
        errors: list[str],
    ) -> str:
        base: dict[str, Any] = {
            "enabled": True,
            "display_name": definition.display_name,
            "group": definition.group,
            "unit": definition.unit,
            "direction": definition.direction,
            "warning": runtime.warning,
            "critical": runtime.critical,
            "severity_enabled": runtime.severity_enabled,
            "samples": [],
            "level": LEVEL_ERROR,
        }
        try:
            if self.host is None:
                samples = self.monitor.query_metric(definition.metric_id)
            else:
                samples = self.monitor.query_metric(
                    definition.metric_id, target=self.host.monitor
                )
        except Exception as exc:  # noqa: BLE001 - 单项失败继续后续指标
            message = f"{definition.metric_id} 查询失败: {exc}"
            errors.append(message)
            findings.append(InspectionFinding(definition.metric_id, LEVEL_ERROR, message))
            snapshot[definition.metric_id] = base
            return LEVEL_ERROR

        if not samples:
            message = f"{definition.metric_id} 无有效数据"
            errors.append(message)
            findings.append(InspectionFinding(definition.metric_id, LEVEL_ERROR, message))
            snapshot[definition.metric_id] = base
            return LEVEL_ERROR

        rendered: list[dict[str, Any]] = []
        levels: list[str] = []
        for sample in samples:
            value = float(sample.value)
            level = _threshold_level(value, runtime, definition)
            levels.append(level)
            rendered.append(
                {
                    "labels": dict(sample.labels),
                    "value": value,
                    "timestamp": sample.timestamp,
                    "level": level,
                }
            )
            if level != LEVEL_NORMAL:
                threshold = runtime.critical if level == LEVEL_CRITICAL else runtime.warning
                findings.append(
                    InspectionFinding(
                        definition.metric_id,
                        level,
                        f"{definition.display_name} "
                        f"{_fmt_value(value, definition.unit)} 达到 {level} 阈值 "
                        f"{_fmt_value(threshold, definition.unit)}",
                        value=value,
                        threshold=threshold,
                    )
                )

        overall = max_level(*levels)
        base.update({"samples": rendered, "level": overall})
        snapshot[definition.metric_id] = base
        return overall

    def _check_active_alerts(
        self,
        definition: MetricDefinition,
        runtime: MetricRuntimeConfig,
        snapshot: dict[str, Any],
        findings: list[InspectionFinding],
        errors: list[str],
    ) -> str:
        base: dict[str, Any] = {
            "enabled": True,
            "display_name": definition.display_name,
            "group": definition.group,
            "unit": definition.unit,
            "direction": definition.direction,
            "warning": runtime.warning,
            "critical": runtime.critical,
            "severity_enabled": runtime.severity_enabled,
            "count": 0,
            "pending": 0,
            "firing": 0,
            "level": LEVEL_ERROR,
        }
        try:
            if self.host is None:
                alerts = self.monitor.active_alerts()
            else:
                alerts = self.monitor.active_alerts(target=self.host.monitor)
        except Exception as exc:  # noqa: BLE001
            message = f"{definition.metric_id} 查询失败: {exc}"
            errors.append(message)
            findings.append(InspectionFinding(definition.metric_id, LEVEL_ERROR, message))
            snapshot[definition.metric_id] = base
            return LEVEL_ERROR

        visible = [a for a in alerts if a.get("state") in ("pending", "firing")]
        count = len(visible)
        level = _threshold_level(float(count), runtime, definition)
        base.update(
            {
                "count": count,
                "pending": sum(1 for a in visible if a.get("state") == "pending"),
                "firing": sum(1 for a in visible if a.get("state") == "firing"),
                "level": level,
            }
        )
        snapshot[definition.metric_id] = base
        if level != LEVEL_NORMAL:
            threshold = runtime.critical if level == LEVEL_CRITICAL else runtime.warning
            findings.append(
                InspectionFinding(
                    definition.metric_id,
                    level,
                    f"活动告警 {count} 条达到 {level} 阈值 {threshold}",
                    value=count,
                    threshold=threshold,
                )
            )
        return level


class MultiHostInspectionChecker:
    """在主机边界串行巡检；坏节点和轮次预算不影响其它节点。"""

    def __init__(
        self,
        monitor: MonitorSource,
        registry: HostRegistry,
        settings: InspectionSettings,
        metrics_config: MetricsConfig,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.monitor = monitor
        self.registry = registry
        self.settings = settings
        self.metrics_config = metrics_config
        self._clock = clock or time.monotonic
        self._cursor = 0

    def run_all(self) -> tuple[InspectionResult, ...]:
        hosts = list(self.registry.enabled())
        if not hosts:
            return ()
        query_cost = max(1, len(self.metrics_config.selected("inspection")) + 1)
        budget_hosts = max(
            1,
            min(
                self.settings.max_hosts_per_run,
                self.settings.max_queries_per_run // query_cost,
            ),
        )
        count = min(len(hosts), budget_hosts)
        selected = [hosts[(self._cursor + offset) % len(hosts)] for offset in range(count)]
        started = self._clock()
        results: list[InspectionResult] = []
        processed = 0
        for host in selected:
            if results and self._clock() - started >= self.settings.round_timeout_seconds:
                break
            results.append(
                InspectionChecker(
                    self.monitor,
                    self.settings,
                    self.metrics_config,
                    host=host,
                ).run_once()
            )
            processed += 1
        self._cursor = (self._cursor + max(processed, 1)) % len(hosts)
        return tuple(results)


class InspectionScheduler:
    """单进程后台调度器；同进程内串行执行，不与 FastAPI 请求线程混用。"""

    def __init__(
        self,
        checker: InspectionChecker,
        settings: InspectionSettings,
        *,
        store: InspectionStore | None = None,
        stop_event: threading.Event | None = None,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.checker = checker
        self.settings = settings
        self.store = store
        self._stop = stop_event or threading.Event()
        self._thread: threading.Thread | None = None
        self._run_lock = threading.Lock()
        self._runtime_provider: Callable[[], tuple[Any, InspectionSettings]] | None = None
        self._wake = threading.Event()
        self.logger = logger_ or get_inspection_logger()
        self.last_run_at = ""
        self.last_level = ""
        self.last_error = ""
        self.last_by_host: dict[str, InspectionResult] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if (not self.settings.enabled and self._runtime_provider is None) or self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="AIOps-Assistant-inspection",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(float(timeout), 0.0))
        if thread is None or not thread.is_alive():
            self._thread = None

    def configure_runtime(
        self, provider: Callable[[], tuple[Any, InspectionSettings]]
    ) -> None:
        """绑定单一调度线程每轮读取的运行快照，而非重启线程。"""
        self._runtime_provider = provider
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def run_once(self) -> InspectionResult:
        with self._run_lock:
            checker = self.checker
            if self._runtime_provider is not None:
                checker, _settings = self._runtime_provider()
            try:
                if hasattr(checker, "run_all"):
                    results = list(checker.run_all())
                else:
                    results = [checker.run_once()]
            except Exception as exc:  # noqa: BLE001 - 单轮异常仍产出 error 结果
                results = [InspectionResult(
                    checked_at=_now_iso(),
                    level=LEVEL_ERROR,
                    snapshot={},
                    findings=(InspectionFinding("inspection", LEVEL_ERROR, str(exc)),),
                    error=f"inspection 执行异常: {exc}",
                )]
            if not results:
                results = [InspectionResult(
                    checked_at=_now_iso(),
                    level=LEVEL_ERROR,
                    error="没有可巡检的启用主机",
                )]
            for result in results:
                self._log(result)
                if result.host_id:
                    self.last_by_host[result.host_id] = result
                if result.level != LEVEL_NORMAL and self.store is not None:
                    try:
                        self.store.record(result)
                    except Exception as exc:  # noqa: BLE001 - 归档失败只降级
                        self.logger.error(
                            "巡检异常归档失败，结果仅保留在日志: %s",
                            exc,
                            exc_info=True,
                        )
            overall = max(results, key=lambda item: LEVEL_ORDER.get(item.level, 0))
            self.last_run_at = max(result.checked_at for result in results)
            self.last_level = overall.level
            self.last_error = "present" if any(result.error for result in results) else ""
            return overall

    def _loop(self) -> None:
        settings = self._runtime_provider()[1] if self._runtime_provider else self.settings
        if settings.enabled and settings.run_on_start:
            self._safe_run_once()
        while not self._stop.is_set():
            settings = self._runtime_provider()[1] if self._runtime_provider else self.settings
            interval = max(int(settings.interval_seconds), 1)
            woke = self._wake.wait(interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            if not woke and settings.enabled:
                self._safe_run_once()

    def _safe_run_once(self) -> None:
        try:
            self.run_once()
        except Exception as exc:  # noqa: BLE001 - 一轮失败不能让后台线程退出
            self.logger.error("巡检执行异常，将在下一周期继续: %s", exc, exc_info=True)

    def _log(self, result: InspectionResult) -> None:
        if result.level == LEVEL_NORMAL:
            level = logging.INFO
        elif result.level == LEVEL_WARNING:
            level = logging.WARNING
        else:
            level = logging.ERROR
        self.logger.log(level, "%s", format_inspection_summary(result))
