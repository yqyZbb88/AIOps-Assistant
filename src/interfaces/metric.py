"""统一指标目录与运行时配置的中性类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class MetricDefinition:
    """代码内置的指标定义；查询表达式只允许代码维护。"""

    metric_id: str
    display_name: str
    group: str
    unit: str
    kind: str
    direction: str
    expr_template: str = ""
    range_expr_template: str | None = None
    base_selector: Mapping[str, str] = field(default_factory=dict)
    group_by: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    legacy_tool_name: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    default_warning: float | None = None
    default_critical: float | None = None
    default_inspection: bool = True
    default_chat: bool = False
    default_alert_analysis: bool = False
    supports_alerts: bool = False


@dataclass(frozen=True)
class MetricSample:
    """一个时序样本。"""

    metric_id: str
    labels: Mapping[str, str]
    value: float
    timestamp: float | None = None


@dataclass(frozen=True)
class MetricSurfaceConfig:
    inspection: bool = False
    chat: bool = False
    alert_analysis: bool = False


@dataclass(frozen=True)
class MetricRuntimeConfig:
    """metrics.yaml 中单个指标的运行配置。"""

    metric_id: str
    enabled: bool = True
    surfaces: MetricSurfaceConfig = field(default_factory=MetricSurfaceConfig)
    warning: float | None = None
    critical: float | None = None
    severity_enabled: bool = False
    selector: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MetricsConfig:
    """完整指标运行配置。"""

    version: int = 1
    instance: str = ""
    max_alert_analysis_metrics: int = 8
    metrics: Mapping[str, MetricRuntimeConfig] = field(default_factory=dict)

    def runtime(self, metric_id: str) -> MetricRuntimeConfig | None:
        return self.metrics.get(metric_id)

    def selected(self, surface: str) -> list[MetricRuntimeConfig]:
        """按配置顺序返回某个使用范围启用的指标。"""
        result: list[MetricRuntimeConfig] = []
        for config in self.metrics.values():
            if not config.enabled:
                continue
            if getattr(config.surfaces, surface):
                result.append(config)
        return result
