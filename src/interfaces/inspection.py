"""固定指标巡检的中性结果模型与存储契约。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

LEVEL_NORMAL = "normal"
LEVEL_WARNING = "warning"
LEVEL_CRITICAL = "critical"
LEVEL_ERROR = "error"

LEVEL_ORDER = {
    LEVEL_NORMAL: 0,
    LEVEL_WARNING: 1,
    LEVEL_CRITICAL: 2,
    LEVEL_ERROR: 3,
}


def max_level(*levels: str) -> str:
    """返回优先级最高的巡检等级。"""
    return max(levels or (LEVEL_NORMAL,), key=lambda level: LEVEL_ORDER.get(level, 0))


class InspectionError(Exception):
    """巡检归档失败。"""


@dataclass(frozen=True)
class InspectionFinding:
    metric: str
    level: str
    message: str
    value: Any = None
    threshold: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "level": self.level,
            "message": self.message,
            "value": self.value,
            "threshold": self.threshold,
        }


@dataclass(frozen=True)
class InspectionResult:
    checked_at: str
    level: str
    snapshot: dict[str, Any] = field(default_factory=dict)
    findings: tuple[InspectionFinding, ...] = ()
    error: str = ""
    record_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.record_id,
            "checked_at": self.checked_at,
            "level": self.level,
            "snapshot": self.snapshot,
            "findings": [finding.to_dict() for finding in self.findings],
            "error": self.error,
        }


class InspectionStore(ABC):
    """异常巡检记录存储抽象。normal 轮次由调用方保证不传入。"""

    @property
    def healthy(self) -> bool:
        return True

    @property
    def last_error(self) -> str | None:
        return None

    @abstractmethod
    def record(self, result: InspectionResult) -> None:
        """写入一条异常巡检记录；写失败抛 InspectionError。"""

    @abstractmethod
    def recent(self, *, limit: int = 50) -> list[InspectionResult]:
        """按时间倒序返回异常巡检记录。"""

    @abstractmethod
    def purge_expired(self) -> int:
        """按 TTL 和容量上限清理，返回删除行数。"""
