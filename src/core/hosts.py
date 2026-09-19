"""按登记主机聚合监控状态。"""
from __future__ import annotations

from typing import Any

from src.interfaces.host import Host, HostRegistry
from src.interfaces.monitor import MonitorSource


class HostStatusService:
    def __init__(self, registry: HostRegistry, monitor: MonitorSource) -> None:
        self.registry = registry
        self.monitor = monitor

    def status(self, host: Host) -> dict[str, Any]:
        exporter = self.monitor.target_status(host.monitor)
        payload: dict[str, Any] = {
            "host": host.to_public_dict(),
            "exporter": exporter,
            "metrics": {},
            "active_alerts": [],
        }
        if exporter.get("state") != "reachable":
            return payload
        for metric_id in ("cpu.usage", "memory.usage", "disk.usage"):
            try:
                samples = self.monitor.query_metric(metric_id, target=host.monitor)
                payload["metrics"][metric_id] = {
                    "state": "ok" if samples else "no_data",
                    "samples": [
                        {
                            "labels": dict(sample.labels),
                            "value": sample.value,
                            "timestamp": sample.timestamp,
                        }
                        for sample in samples
                    ],
                }
            except Exception:
                payload["metrics"][metric_id] = {
                    "state": "query_error",
                    "samples": [],
                }
        try:
            payload["active_alerts"] = self.monitor.active_alerts(target=host.monitor)
        except Exception:
            payload["alerts_state"] = "query_error"
        return payload
