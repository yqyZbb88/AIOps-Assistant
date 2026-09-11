"""状态日志契约：告警 / 处置 / AI 建议三类归档记录（中性类型，无第三方依赖）。

背景：这三份数据原先**全是重启即丢**——告警只在 `AlertStore` 的 `deque(maxlen=100)` 里，
处置结果与 AI 建议只随 `/alert` 的 HTTP 响应返回一次就蒸发（另有一次 `print()`）。
本模块描述落盘后的形状，实现见 [src/storage/history.py](../storage/history.py)。

三张表靠 `alert_key` 关联（见 `storage/models.py::Alert.alert_key`）：
一条告警可能命中多条规则（多条处置）或没有规则（转 AI 分析），所以是一对多。

安全约定（**落盘会把这些内容写到磁盘上，比只进 console 严重得多**）：
- `detail` 里可能含远端命令的 stdout（执行器把整个 result 拼了进去），
  而既有不变量是「整段命令输出不落日志」——故存储层在写之前**必须截断**；
- `advice` 是模型自由文本，同样定长度上限，避免一次超长回复撑爆库。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence

#: 归档字段的长度上限（存储层写入前强制截断，防远端输出 / 模型长文本撑爆库）
MAX_DETAIL_CHARS = 500
MAX_ADVICE_CHARS = 4000
MAX_SUMMARY_CHARS = 500


class HistoryError(Exception):
    """状态日志存储失败。"""


@dataclass(frozen=True)
class AlertRecord:
    """一条告警的归档记录（webhook 收到的原文摘要）。"""

    alert_key: str
    alertname: str
    severity: str = ""
    instance: str = ""
    summary: str = ""
    status: str = "firing"
    starts_at: str = ""
    ends_at: str = ""
    received_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_key": self.alert_key,
            "alertname": self.alertname,
            "severity": self.severity,
            "instance": self.instance,
            "summary": self.summary,
            "status": self.status,
            "starts_at": self.starts_at,
            "ends_at": self.ends_at,
            "received_at": self.received_at,
        }


@dataclass(frozen=True)
class RemediationRecord:
    """一条「告警 → 规则 → 动作」的处置结果。"""

    alert_key: str
    alertname: str
    severity: str = ""
    instance: str = ""
    matched_rule: str = ""
    action: str | None = None
    status: str = ""
    detail: str = ""
    executed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_key": self.alert_key,
            "alertname": self.alertname,
            "severity": self.severity,
            "instance": self.instance,
            "matched_rule": self.matched_rule,
            "action": self.action,
            "status": self.status,
            "detail": self.detail,
            "executed_at": self.executed_at,
        }


@dataclass(frozen=True)
class AdviceRecord:
    """一条 AI 根因分析建议（无规则命中的告警才会产生）。"""

    alert_key: str
    alertname: str
    severity: str = ""
    instance: str = ""
    advice: str = ""
    analyzed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_key": self.alert_key,
            "alertname": self.alertname,
            "severity": self.severity,
            "instance": self.instance,
            "advice": self.advice,
            "analyzed_at": self.analyzed_at,
        }


class HistoryStore(ABC):
    """告警 / 处置 / AI 建议的归档存储抽象。

    实现约定与 `SessionStore` 一致：**写失败抛异常**（由调用方决定降级），
    `healthy` / `last_error` 供 /health 暴露降级状态。
    """

    @property
    def healthy(self) -> bool:
        """存储是否正常（写失败后置 False）。"""
        return True

    @property
    def last_error(self) -> str | None:
        return None

    @abstractmethod
    def record_batch(
        self,
        *,
        alerts: Sequence[AlertRecord] = (),
        remediations: Sequence[RemediationRecord] = (),
        advice: Sequence[AdviceRecord] = (),
    ) -> None:
        """把一次告警处理的产出**同批写入**三张表。

        **一次调用 = 一个事务**：三张表要么都落、要么都不落，避免出现
        「告警写了、处置没写」的半批记录——那种记录比完全没有更难排查。
        """

    @abstractmethod
    def recent_alerts(self, *, limit: int = 50) -> list[AlertRecord]:
        """最近的告警归档，新的在前。"""

    @abstractmethod
    def recent_remediations(self, *, limit: int = 50) -> list[RemediationRecord]:
        """最近的处置归档，新的在前。"""

    @abstractmethod
    def recent_advice(self, *, limit: int = 50) -> list[AdviceRecord]:
        """最近的 AI 建议归档，新的在前。"""

    @abstractmethod
    def purge_expired(self) -> int:
        """按 TTL + 容量上限清理三张表，返回清理的行数。"""
