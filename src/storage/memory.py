"""最近告警的内存缓冲（AlertStore）。

承接原 ops_bot/alerts.py 中 handle_alert 的「记录 + 日志 + deque」逻辑，
收敛为可注入的类，便于 core/agent 装配与单测替换（进程重启即清空）。
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any

from src.storage.models import Alert

logger = logging.getLogger("ops_agent.alert_store")


class AlertStore:
    """最近告警的内存缓冲 + 记录逻辑。

    - record(alert)：单条告警 → 结构化摘要并写入缓冲 + 按状态记日志（firing WARNING，
      resolved INFO），返回摘要；
    - recent(limit)：返回最近接收记录，新的在前。
    """

    def __init__(self, maxlen: int = 100) -> None:
        self.maxlen = max(maxlen, 1)
        self._buffer: deque[dict[str, Any]] = deque(maxlen=self.maxlen)

    def record(self, alert: Alert) -> dict[str, Any]:
        """记录一条告警到缓冲，返回其结构化摘要。"""
        record: dict[str, Any] = {
            "status": alert.status,
            "alertname": alert.alertname,
            "severity": alert.severity,
            "instance": alert.instance,
            "summary": alert.annotations.get("summary", ""),
            "startsAt": alert.startsAt,
            "received_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._buffer.append(record)

        if alert.is_resolved:
            logger.info(
                "告警已解决 [%s] severity=%s instance=%s %s",
                alert.alertname, alert.severity, alert.instance, record["summary"],
            )
        else:
            logger.warning(
                "收到告警 [%s] severity=%s instance=%s %s",
                alert.alertname, alert.severity, alert.instance, record["summary"],
            )
        return record

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """返回最近接收的告警记录，新的在前；limit 夹取到 [1, maxlen]。"""
        limit = max(1, min(int(limit), self.maxlen))
        return list(self._buffer)[-limit:][::-1]

    def __len__(self) -> int:
        return len(self._buffer)
