"""进程、监控源、存储与巡检调度器的健康快照。"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from src.interfaces.monitor import MonitorSource
from src.utils.time import now_iso


class HealthService:
    def __init__(
        self,
        monitor: MonitorSource,
        *,
        storage_enabled: bool,
        stores: Callable[[], list[Any]],
        inspection_enabled: bool,
        scheduler: Callable[[], Any],
        version: str,
    ) -> None:
        self.monitor = monitor
        self.storage_enabled = storage_enabled
        self.stores = stores
        self.inspection_enabled = inspection_enabled
        self.scheduler = scheduler
        self.version = version
        self._lock = threading.Lock()
        self._prometheus = "unknown"
        self._checked_at = ""

    def snapshot(self, *, probe: bool = False) -> dict[str, Any]:
        if probe:
            try:
                self.monitor.check_health()
                prometheus = "ok"
            except Exception:
                prometheus = "unreachable"
            with self._lock:
                self._prometheus = prometheus
                self._checked_at = now_iso()
        with self._lock:
            prometheus = self._prometheus
            checked_at = self._checked_at

        stores = self.stores()
        storage_detail = "disabled" if not self.storage_enabled else (
            "ok" if stores and all(store is not None and store.healthy for store in stores)
            else "degraded"
        )
        scheduler = self.scheduler() if self.inspection_enabled else None
        inspection = "disabled" if scheduler is None else (
            "running" if scheduler.running else "stopped"
        )
        ready = prometheus == "ok" and inspection != "stopped"
        degraded = storage_detail == "degraded" or prometheus == "unreachable" or inspection == "stopped"
        return {
            "status": "degraded" if degraded else "ok",
            "storage": "degraded" if storage_detail == "degraded" else "ok",
            "version": self.version,
            "ready": ready,
            "components": {
                "process": {"status": "ok"},
                "prometheus": {"status": prometheus, "checked_at": checked_at},
                "storage": {"status": storage_detail},
                "inspection": {
                    "status": inspection,
                    "last_run_at": getattr(scheduler, "last_run_at", "") if scheduler else "",
                    "last_level": getattr(scheduler, "last_level", "") if scheduler else "",
                },
            },
        }
