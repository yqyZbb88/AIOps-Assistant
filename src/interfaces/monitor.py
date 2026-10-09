"""MonitorSource：监控源抽象接口。

core（聊天兜底 / 告警分析上下文）与 agent 工具只依赖本接口取数，
不关心具体监控后端（Prometheus / 其它）。查询失败统一抛 MonitorError，
由调用方（工具 / handler）转成友好提示；「无数据」则返回 None / 空列表。

TrendSummary：单个指标近 N 分钟趋势汇总
    {"latest": float|None, "min": ..., "max": ..., "avg": ..., "points": int}
points 表示摘要对应序列的有效采样点数；series 保留各设备的 labels、values=[时间戳,值]。
无效/缺失值为 None；state 区分 ok、no_data、not_applicable。查询失败抛 MonitorError。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Mapping

from src.interfaces.host import MonitorTarget
from src.interfaces.metric import MetricSample

# 单个指标的近端趋势汇总结构（见 module docstring）
TrendSummary = dict[str, Any]


class MonitorError(Exception):
    """监控数据源查询失败（连接失败 / 返回错误 / 数据异常）。"""


class MetricNotApplicable(MonitorError):
    """目标不具备此项资源，例如未启用 Swap；不表示主机故障。"""


class MonitorSource(ABC):
    """监控源接口：聊天 / 工具 / 告警分析共用的取数契约。"""

    def check_health(self) -> None:
        """检查后端就绪状态；成功无返回，失败抛 ``MonitorError``。"""
        raise MonitorError("监控源不支持健康检查")

    def query_metric(
        self,
        metric_id: str,
        *,
        selector: Mapping[str, str] | None = None,
        target: MonitorTarget | None = None,
    ) -> list[MetricSample]:
        """通用即时指标查询；默认实现表示当前监控源未支持。

        `selector` **只用于补充 `instance`**（同一指标按实例取数）：标签范围的正主是
        指标定义与 metrics.yaml 里的 selector，调用方不得用它替换底层查询文本。
        所有 kind（含 alert_count）都必须认这个参数。
        """
        raise MonitorError(f"监控源不支持通用指标查询: {metric_id}")

    def query_metric_trend(
        self,
        metric_id: str,
        instance: str | None = None,
        *,
        minutes: int = 60,
        step: str | None = None,
        selector: Mapping[str, str] | None = None,
        target: MonitorTarget | None = None,
    ) -> TrendSummary:
        """通用指标趋势查询；默认实现表示当前监控源未支持。"""
        raise MonitorError(f"监控源不支持通用趋势查询: {metric_id}")

    def target_status(self, target: MonitorTarget) -> dict[str, Any]:
        """查询登记目标的 Exporter 采集状态。"""
        raise MonitorError("监控源不支持按主机查询 Exporter 状态")

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
    def active_alerts(
        self, *, target: MonitorTarget | None = None
    ) -> list[dict[str, Any]]:
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
