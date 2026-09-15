"""固定指标巡检：阈值判断、汇总格式化与后台调度。"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from src.interfaces.inspection import (
    LEVEL_CRITICAL,
    LEVEL_ERROR,
    LEVEL_NORMAL,
    LEVEL_WARNING,
    InspectionFinding,
    InspectionResult,
    InspectionStore,
    max_level,
)
from src.interfaces.monitor import MonitorSource
from src.utils.config_loader import InspectionSettings
from src.utils.logger import get_inspection_logger

logger = logging.getLogger("ops_agent.inspection.runner")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _percent_level(value: float, *, warning: float, critical: float) -> str:
    if value >= critical:
        return LEVEL_CRITICAL
    if value >= warning:
        return LEVEL_WARNING
    return LEVEL_NORMAL


def _fmt_percent(value: Any) -> str:
    return f"{value:.1f}%" if isinstance(value, (int, float)) else str(value)


def format_inspection_summary(result: InspectionResult) -> str:
    """把一轮结果压成一条可读日志，保留指标值、阈值和错误信息。"""
    parts = [f"[巡检] level={result.level}", f"checked_at={result.checked_at}"]
    snap = result.snapshot

    for key, label in (("cpu", "cpu"), ("memory", "memory")):
        item = snap.get(key) or {}
        if not item.get("enabled"):
            continue
        parts.append(
            f"{label}={_fmt_percent(item.get('value'))}"
            f"(level={item.get('level')},warn>={item.get('warning')},crit>={item.get('critical')})"
        )

    disk = snap.get("disk") or {}
    if disk.get("enabled"):
        mounts = disk.get("mounts") or []
        shown = ", ".join(
            f"{m.get('mountpoint')}={_fmt_percent(m.get('usage'))}({m.get('level')})"
            for m in mounts
        ) or "无数据"
        parts.append(
            f"disk=[{shown}](warn>={disk.get('warning')},crit>={disk.get('critical')})"
        )

    alerts = snap.get("active_alerts") or {}
    if alerts.get("enabled"):
        parts.append(
            f"active_alerts={alerts.get('count')}"
            f"(level={alerts.get('level')},warn>={alerts.get('warning_count')},"
            f"crit>={alerts.get('critical_count')})"
        )

    if result.error:
        parts.append(f"errors={result.error}")
    return " | ".join(parts)


class InspectionChecker:
    """按配置顺序执行内置指标的只读巡检。"""

    def __init__(
        self,
        monitor: MonitorSource,
        settings: InspectionSettings,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.monitor = monitor
        self.settings = settings
        self._clock = clock or _now_iso

    def run_once(self) -> InspectionResult:
        checked_at = self._clock()
        snapshot: dict[str, Any] = {}
        findings: list[InspectionFinding] = []
        errors: list[str] = []
        levels: list[str] = []

        checks = self.settings.checks
        if checks.cpu.enabled:
            levels.append(
                self._check_percentage(
                    "cpu", self.monitor.cpu_usage, checks.cpu, snapshot, findings, errors
                )
            )
        else:
            snapshot["cpu"] = {"enabled": False}

        if checks.memory.enabled:
            levels.append(
                self._check_percentage(
                    "memory", self.monitor.memory_usage, checks.memory, snapshot, findings, errors
                )
            )
        else:
            snapshot["memory"] = {"enabled": False}

        if checks.disk.enabled:
            levels.append(self._check_disk(checks.disk, snapshot, findings, errors))
        else:
            snapshot["disk"] = {"enabled": False}

        if checks.active_alerts.enabled:
            levels.append(self._check_active_alerts(snapshot, findings, errors))
        else:
            snapshot["active_alerts"] = {"enabled": False}

        overall = max_level(*levels, LEVEL_ERROR if errors else LEVEL_NORMAL)
        return InspectionResult(
            checked_at=checked_at,
            level=overall,
            snapshot=snapshot,
            findings=tuple(findings),
            error="; ".join(errors),
        )

    def _check_percentage(
        self,
        metric: str,
        getter: Callable[[], float | None],
        check,
        snapshot: dict[str, Any],
        findings: list[InspectionFinding],
        errors: list[str],
    ) -> str:
        base = {
            "enabled": True,
            "warning": check.warning,
            "critical": check.critical,
            "value": None,
            "level": LEVEL_ERROR,
        }
        try:
            value = getter()
        except Exception as exc:  # noqa: BLE001 - 单项失败不阻断其它指标
            message = f"{metric} 查询失败: {exc}"
            errors.append(message)
            findings.append(InspectionFinding(metric, LEVEL_ERROR, message))
            snapshot[metric] = base
            return LEVEL_ERROR

        try:
            numeric = None if value is None else float(value)
        except (TypeError, ValueError):
            numeric = None
        if numeric is None or not math.isfinite(numeric):
            message = f"{metric} 无有效数据"
            errors.append(message)
            findings.append(InspectionFinding(metric, LEVEL_ERROR, message))
            snapshot[metric] = base
            return LEVEL_ERROR

        value = numeric
        level = _percent_level(value, warning=check.warning, critical=check.critical)
        base.update({"value": value, "level": level})
        snapshot[metric] = base
        if level != LEVEL_NORMAL:
            threshold = check.critical if level == LEVEL_CRITICAL else check.warning
            findings.append(
                InspectionFinding(
                    metric,
                    level,
                    f"{metric} 使用率 {_fmt_percent(value)} 达到 {level} 阈值 {threshold}%",
                    value=value,
                    threshold=threshold,
                )
            )
        return level

    def _check_disk(
        self,
        check,
        snapshot: dict[str, Any],
        findings: list[InspectionFinding],
        errors: list[str],
    ) -> str:
        base = {
            "enabled": True,
            "warning": check.warning,
            "critical": check.critical,
            "mounts": [],
            "level": LEVEL_ERROR,
        }
        try:
            mounts = self.monitor.disk_usage()
        except Exception as exc:  # noqa: BLE001
            message = f"disk 查询失败: {exc}"
            errors.append(message)
            findings.append(InspectionFinding("disk", LEVEL_ERROR, message))
            snapshot["disk"] = base
            return LEVEL_ERROR

        if not mounts:
            message = "disk 无有效数据"
            errors.append(message)
            findings.append(InspectionFinding("disk", LEVEL_ERROR, message))
            snapshot["disk"] = base
            return LEVEL_ERROR

        levels: list[str] = []
        rendered: list[dict[str, Any]] = []
        for item in mounts:
            try:
                usage = float(item["usage"])
                mountpoint = str(item.get("mountpoint") or "unknown")
                if not math.isfinite(usage):
                    raise ValueError("usage 不是有限数字")
            except (KeyError, TypeError, ValueError) as exc:
                message = f"disk 挂载点数据无效: {item!r} ({exc})"
                errors.append(message)
                findings.append(InspectionFinding("disk", LEVEL_ERROR, message, value=item))
                levels.append(LEVEL_ERROR)
                continue
            level = _percent_level(usage, warning=check.warning, critical=check.critical)
            levels.append(level)
            rendered.append({"mountpoint": mountpoint, "usage": usage, "level": level})
            if level != LEVEL_NORMAL:
                threshold = check.critical if level == LEVEL_CRITICAL else check.warning
                findings.append(
                    InspectionFinding(
                        "disk",
                        level,
                        f"磁盘 {mountpoint} 使用率 {_fmt_percent(usage)} 达到 {level} 阈值 {threshold}%",
                        value=usage,
                        threshold=threshold,
                    )
                )

        overall = max_level(*levels, LEVEL_ERROR if errors else LEVEL_NORMAL)
        base.update({"mounts": rendered, "level": overall})
        snapshot["disk"] = base
        return overall

    def _check_active_alerts(
        self,
        snapshot: dict[str, Any],
        findings: list[InspectionFinding],
        errors: list[str],
    ) -> str:
        check = self.settings.checks.active_alerts
        base = {
            "enabled": True,
            "warning_count": check.warning_count,
            "critical_count": check.critical_count,
            "count": 0,
            "pending": 0,
            "firing": 0,
            "level": LEVEL_ERROR,
        }
        try:
            alerts = self.monitor.active_alerts()
        except Exception as exc:  # noqa: BLE001
            message = f"active_alerts 查询失败: {exc}"
            errors.append(message)
            findings.append(InspectionFinding("active_alerts", LEVEL_ERROR, message))
            snapshot["active_alerts"] = base
            return LEVEL_ERROR

        visible = [a for a in alerts if a.get("state") in ("pending", "firing")]
        count = len(visible)
        if count >= check.critical_count:
            level = LEVEL_CRITICAL
            threshold = check.critical_count
        elif count >= check.warning_count:
            level = LEVEL_WARNING
            threshold = check.warning_count
        else:
            level = LEVEL_NORMAL
            threshold = None

        base.update(
            {
                "count": count,
                "pending": sum(1 for a in visible if a.get("state") == "pending"),
                "firing": sum(1 for a in visible if a.get("state") == "firing"),
                "level": level,
            }
        )
        snapshot["active_alerts"] = base
        if level != LEVEL_NORMAL:
            findings.append(
                InspectionFinding(
                    "active_alerts",
                    level,
                    f"活动告警 {count} 条达到 {level} 阈值 {threshold}",
                    value=count,
                    threshold=threshold,
                )
            )
        return level


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
        self.logger = logger_ or get_inspection_logger()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self.settings.enabled or self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="ops-agent-inspection",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(float(timeout), 0.0))
        self._thread = None

    def run_once(self) -> InspectionResult:
        """执行一轮；日志和异常归档失败都不能让调度线程退出。"""
        with self._run_lock:
            try:
                result = self.checker.run_once()
            except Exception as exc:  # noqa: BLE001 - 单轮异常仍产出 error 结果
                result = InspectionResult(
                    checked_at=_now_iso(),
                    level=LEVEL_ERROR,
                    snapshot={},
                    findings=(InspectionFinding("inspection", LEVEL_ERROR, str(exc)),),
                    error=f"inspection 执行异常: {exc}",
                )
            self._log(result)
            if result.level != LEVEL_NORMAL and self.store is not None:
                try:
                    self.store.record(result)
                except Exception as exc:  # noqa: BLE001 - 归档失败只降级
                    self.logger.error("巡检异常归档失败，结果仅保留在日志: %s", exc, exc_info=True)
            return result

    def _loop(self) -> None:
        if self.settings.run_on_start:
            self._safe_run_once()
        interval = max(int(self.settings.interval_seconds), 1)
        while not self._stop.wait(interval):
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
