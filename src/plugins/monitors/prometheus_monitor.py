"""Prometheus 监控源插件（实现 MonitorSource）。

承接原 ops_bot/prometheus.py 的全部查询逻辑（即时 / 范围 / 活动告警），
改造成「注入 base_url + timeout」的类实现，PromQL 表达式留在插件内不泄漏到 core。
查询失败统一抛 MonitorError；「无数据」返回 None / 空列表 / points=0 汇总。
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from src.interfaces.monitor import MonitorError, MonitorSource

logger = logging.getLogger("ops_agent.prometheus")

# PromQL 表达式集中管理，便于按需调整统计口径
# 三个"实时用量"查询统一在尾部做 "and on(instance) up == 1" 门控：
#   目标机一断线，up 会在下一个抓取周期立刻变 0，而指标旧样本仍会被 Prometheus 保留
#   到滑出 lookback/窗口（约 5 分钟）——门控让查询在 up=0 时立即返回空 → "暂无数据"，
#   避免把断线前的最后一次读数误当实时值。
#   趋势查询（_cpu_expr 等）是历史取数、不在此列；主动告警走 up 本身，天然即时。
# CPU 特意用 irate 而非 rate：与告警规则 HighCPUUsage（irate[5m] > 85）同口径，
#   让 AI 报的数值与触发告警的数值是同一套定义，能立刻反映瞬时冲高而非 5 分钟平滑均值。
_QUERY_CPU = (
    '100 - avg(irate(node_cpu_seconds_total{mode="idle"}[5m]) '
    'and on(instance) up == 1) * 100'
)
_QUERY_MEMORY = (
    "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) "
    "* 100 and on(instance) up == 1"
)
# 与告警规则 HighDiskUsage 口径一致：排除 tmpfs/overlay 虚拟挂载点
_QUERY_DISK = (
    '100 - (node_filesystem_avail_bytes{fstype!~"tmpfs|overlay"} / '
    'node_filesystem_size_bytes) * 100 and on(instance) up == 1'
)

# 趋势查询用的按 instance 过滤表达式（node_exporter 指标，与告警规则口径一致）


def _cpu_expr(instance: str) -> str:
    return (
        '100 - avg by(instance)(rate(node_cpu_seconds_total{mode="idle", '
        f'instance="{instance}"}}[5m])) * 100'
    )


def _mem_expr(instance: str) -> str:
    return (
        f'(1 - node_memory_MemAvailable_bytes{{instance="{instance}"}} / '
        f'node_memory_MemTotal_bytes{{instance="{instance}"}}) * 100'
    )


def _disk_expr(instance: str) -> str:
    # 与 alerts.yml HighDiskUsage 口径一致：排除 tmpfs/overlay 虚拟挂载点
    return (
        '100 - (node_filesystem_avail_bytes{'
        f'instance="{instance}", fstype!~"tmpfs|overlay"}} / '
        'node_filesystem_size_bytes{'
        f'instance="{instance}", fstype!~"tmpfs|overlay"}}) * 100'
    )


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """将使用率限制在 [low, high]，避免 Prometheus 计算误差导致越界。"""
    return max(low, min(high, value))


class PrometheusMonitor(MonitorSource):
    """Prometheus HTTP API 监控源。直接 requests 调用，无第三方客户端依赖。"""

    #: 注册名（registry / settings.monitor.provider 用）
    name = "prometheus"
    description = "Prometheus（node_exporter）指标查询与活动告警"

    def __init__(self, base_url: str = "http://localhost:9090", timeout: int = 5) -> None:
        self.base_url = (base_url or "http://localhost:9090").rstrip("/")
        self.timeout = timeout

    # -- 底层查询 ---------------------------------------------------------
    def _request(self, path: str, params: dict[str, Any]) -> Any:
        """对 /api/v1/<path> 发一次 GET，返回 data 段；异常统一抛 MonitorError。"""
        url = f"{self.base_url}/api/v1/{path}"
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise MonitorError(f"Prometheus 请求失败: {exc}") from exc

        payload = resp.json()
        if payload.get("status") != "success":
            raise MonitorError(f"Prometheus 返回错误: {payload.get('error', '未知错误')}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise MonitorError("Prometheus 返回数据格式异常")
        return data

    def _query(self, expr: str) -> list[dict[str, Any]]:
        """执行一次即时查询，返回 result 序列列表。"""
        result = self._request("query", {"query": expr}).get("result", [])
        if not isinstance(result, list):
            raise MonitorError("Prometheus 返回数据格式异常")
        return result

    def _query_range(self, expr: str, minutes: int, step: str) -> list[dict[str, Any]]:
        """执行一次范围查询，返回 result 序列列表（时间窗 [now-minutes, now]）。"""
        now = time.time()
        data = self._request(
            "query_range",
            {
                "query": expr,
                "start": int(now - minutes * 60),
                "end": int(now),
                "step": step,
            },
        )
        result = data.get("result", [])
        if not isinstance(result, list):
            raise MonitorError("Prometheus 返回数据格式异常")
        return result

    # -- MonitorSource：当前用量 ------------------------------------------
    @staticmethod
    def _first_value(result: list[dict[str, Any]]) -> float | None:
        """取即时查询第一条序列数值；空返回 None，解析失败抛 MonitorError。"""
        if not result:
            return None
        try:
            return float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise MonitorError("Prometheus 返回数据无法解析") from exc

    def cpu_usage(self) -> float | None:
        """查询 CPU 使用率（%），返回 [0,100]；无数据返回 None。"""
        value = self._first_value(self._query(_QUERY_CPU))
        return None if value is None else _clamp(value)

    def memory_usage(self) -> float | None:
        """查询内存使用率（%），返回 [0,100]；无数据返回 None。"""
        value = self._first_value(self._query(_QUERY_MEMORY))
        return None if value is None else _clamp(value)

    def disk_usage(self) -> list[dict[str, Any]]:
        """各挂载点磁盘使用率，按使用率降序；无数据返回空列表。"""
        mounts: list[dict[str, Any]] = []
        for item in self._query(_QUERY_DISK):
            try:
                usage = _clamp(float(item["value"][1]))
            except (KeyError, IndexError, TypeError, ValueError):
                logger.warning("磁盘指标单条数据解析失败，已跳过: %s", item)
                continue
            mountpoint = item.get("metric", {}).get("mountpoint", "未知挂载点")
            mounts.append({"mountpoint": mountpoint, "usage": usage})
        mounts.sort(key=lambda m: m["usage"], reverse=True)
        return mounts

    # -- MonitorSource：活动告警 ------------------------------------------
    def active_alerts(self) -> list[dict[str, Any]]:
        """查询当前 pending / firing 告警（/api/v1/alerts），firing 在前。"""
        data = self._request("alerts", {})
        records: list[dict[str, Any]] = []
        for item in data.get("alerts", []):
            labels = item.get("labels", {})
            annotations = item.get("annotations", {})
            raw_value = item.get("value")
            try:
                value = float(raw_value) if raw_value not in (None, "") else None
            except (TypeError, ValueError):
                value = None
            records.append(
                {
                    "alertname": labels.get("alertname", "unknown"),
                    "severity": labels.get("severity", "none"),
                    "instance": labels.get("instance", "unknown"),
                    "state": item.get("state", "unknown"),
                    "activeAt": item.get("activeAt", ""),
                    "value": value,
                    "summary": annotations.get("summary", ""),
                    "description": annotations.get("description", ""),
                }
            )

        state_rank = {"firing": 0, "pending": 1}
        records.sort(key=lambda r: (state_rank.get(r["state"], 9), r["alertname"]))
        return records

    # -- MonitorSource：实例近端趋势（告警 AI 分析数据源） -----------------
    def instance_trend(
        self,
        instance: str,
        *,
        minutes: int = 60,
        step: str = "5m",
    ) -> dict[str, dict[str, Any]]:
        """组装 {指标中文名: 汇总}；单指标查询失败/无数据记日志并给 points=0。"""
        exprs: list[tuple[str, str]] = [
            ("CPU使用率", _cpu_expr(instance)),
            ("内存使用率", _mem_expr(instance)),
            ("磁盘使用率", _disk_expr(instance)),
        ]
        trend: dict[str, dict[str, Any]] = {}
        for title, expr in exprs:
            trend[title] = self._one_metric_trend(expr, minutes=minutes, step=step)
        return trend

    def _one_metric_trend(self, expr: str, minutes: int, step: str) -> dict[str, Any]:
        """对单个表达式做范围查询并汇总；异常/无数据给 points=0 空汇总。"""
        try:
            series = self._query_range(expr, minutes=minutes, step=step)
        except MonitorError as exc:
            logger.debug("告警趋势取数失败(%s): %s", expr, exc)
            return {"latest": None, "min": None, "max": None, "avg": None, "points": 0}
        if not series:
            return {"latest": None, "min": None, "max": None, "avg": None, "points": 0}
        return _summarize_values(series[0].get("values", []))


def _summarize_values(values: list[list[Any]]) -> dict[str, Any]:
    """把 range 查询的 values [[ts, val], ...] 汇总为 latest/min/max/avg/points。"""
    nums = []
    for _, raw in values:
        if raw in (None, ""):
            continue
        try:
            nums.append(_clamp(float(raw)))
        except (TypeError, ValueError):
            continue
    if not nums:
        return {"latest": None, "min": None, "max": None, "avg": None, "points": 0}
    return {
        "latest": nums[-1],
        "min": min(nums),
        "max": max(nums),
        "avg": sum(nums) / len(nums),
        "points": len(nums),
    }
