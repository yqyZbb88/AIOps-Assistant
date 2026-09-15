"""MonitorSource：监控源抽象接口。

core（聊天兜底 / 告警分析上下文）与 agent 工具只依赖本接口取数，
不关心具体监控后端（Prometheus / 其它）。查询失败统一抛 MonitorError，
由调用方（工具 / handler）转成友好提示；「无数据」则返回 None / 空列表。

TrendSummary：单个指标近 N 分钟趋势汇总
    {"latest": float|None, "min": ..., "max": ..., "avg": ..., "points": int}
points 表示有效采样点数；全 None + points=0 表示该指标无数据。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from src.interfaces.metric import MetricSample

# 单个指标的近端趋势汇总结构（见 module docstring）
TrendSummary = dict[str, Any]


class MonitorError(Exception):
    """监控数据源查询失败（连接失败 / 返回错误 / 数据异常）。"""


class MonitorSource(ABC):
    """监控源接口：聊天 / 工具 / 告警分析共用的取数契约。"""

    def query_metric(
        self,
        metric_id: str,
        *,
        selector: Mapping[str, str] | None = None,
    ) -> list[MetricSample]:
        """通用即时指标查询；默认实现表示当前监控源未支持。"""
        raise MonitorError(f"监控源不支持通用指标查询: {metric_id}")

    def query_metric_trend(
        self,
        metric_id: str,
        instance: str,
        *,
        minutes: int = 60,
        step: str = "5m",
        selector: Mapping[str, str] | None = None,
    ) -> TrendSummary:
        """通用指标趋势查询；默认实现表示当前监控源未支持。"""
        raise MonitorError(f"监控源不支持通用趋势查询: {metric_id}")

    @abstractmethod
    def cpu_usage(self) -> float | None:
        """当前 CPU 使用率（%），[0,100]；无数据返回 None。"""

    @abstractmethod
    def memory_usage(self) -> float | None:
        """当前内存使用率（%），[0,100]；无数据返回 None。"""

    @abstractmethod
    def disk_usage(self) -> list[dict[str, Any]]:
        """各挂载点磁盘使用率，形如 [{"mountpoint": "/", "usage": 45.2}, ...]
        按使用率降序；无数据返回空列表。"""

    @abstractmethod
    def active_alerts(self) -> list[dict[str, Any]]:
        """当前 pending / firing 告警记录（firing 在前），格式与历史一致：
        [{"alertname","severity","instance","state","activeAt","value",
          "summary","description"}, ...]；无活动告警返回空列表。"""

    @abstractmethod
    def instance_trend(
        self,
        instance: str,
        *,
        minutes: int = 60,
        step: str = "5m",
    ) -> dict[str, TrendSummary]:
        """实例近 minutes 分钟的趋势汇总（供告警 AI 分析）。

        返回键固定的中文指标名 → 汇总：
            {"CPU使用率": TrendSummary, "内存使用率": TrendSummary,
             "磁盘使用率": TrendSummary}
        单指标查询失败 / 无数据时对应汇总为全 None + points=0（尽力而为不抛错）。
        """
