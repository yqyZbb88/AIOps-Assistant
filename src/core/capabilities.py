"""监控能力单一实现：查询 + 格式化收敛为纯函数（Agent 工具与关键词兜底共用）。

背景：CPU / 内存 / 磁盘 / 告警 四类监控查询在两条链路上各实现过一次——
    Agent 工具（agent/tools.py 的 @tool）与无 LLM 兜底（core/chat.py 的关键词 handler），
    逻辑重复、措辞还漂移（一边"CPU 使用率：45.0%"，一边"当前 CPU 使用率：45.0%"）。
本模块把「取数 + 转可读文本」收编成 4 个纯函数，两条链路都转发到这里，
产出**同一套规范文案**；以后改措辞只改本模块，Agent 与兜底同步生效。

约束：
- 顶层不 import langchain：无 langchain 的兜底链路也能 import 本模块直接调用；
- 每个函数接收注入的 MonitorSource，内部捕获 MonitorError → 返回可读失败文案，
  绝不抛异常（监控挂掉 / 命令失败只是"查询失败"可读返回，不中断 Agent）；
- 文案即规范：成功 `CPU 使用率：45.0%` / 失败 `查询 CPU 使用率失败：…` /
  无数据 `…暂无数据（请确认 Node Exporter 已采集）…`。

扩展方式：新增监控能力 → 在本模块加 `xxx_text(monitor)` 纯函数，再到
agent/tools.py 的 build_tools 与 core/chat.py 的规则各转发一行。
"""
from __future__ import annotations

import logging
from typing import Callable

from src.interfaces.metric import MetricDefinition
from src.interfaces.monitor import MonitorError, MonitorSource
from src.utils.freshness import epoch_to_iso, freshness_note

logger = logging.getLogger("ops_agent.capabilities")

# 指标无数据时的统一说明（Node Exporter 未采集 / Prometheus 无序列）
_NO_DATA_HINT = "暂无数据（请确认 Node Exporter 已采集）"


def _pct(value: float | None) -> str:
    """百分比格式化；None（暂无数据）返回统一说明。"""
    if value is None:
        return _NO_DATA_HINT
    return f"{value:.1f}%"


def _usage_text(getter: Callable[[], float | None], name: str) -> str:
    """单百分比指标的共用实现：取数 → MonitorError 转失败文案 → None 转暂无数据。

    getter 传 MonitorSource 的取值方法本身（如 monitor.cpu_usage），
    不走 getattr 字符串派发——拼错方法名由类型检查/IDE 当场发现，而非运行时才炸。
    """
    try:
        value = getter()
    except MonitorError as exc:
        logger.warning("查询 %s 失败: %s", name, exc)
        return f"查询 {name}失败：{exc}"
    if value is None:
        return f"{name}：{_NO_DATA_HINT}"
    return f"{name}：{value:.1f}%"


def cpu_usage_text(monitor: MonitorSource) -> str:
    """查询 + 格式化当前 CPU 使用率（%）。失败返回"查询 CPU 使用率失败：…"。"""
    return _usage_text(monitor.cpu_usage, "CPU 使用率")


def memory_usage_text(monitor: MonitorSource) -> str:
    """查询 + 格式化当前内存使用率（%）。失败返回"查询 内存使用率失败：…"。"""
    return _usage_text(monitor.memory_usage, "内存使用率")


def disk_usage_text(monitor: MonitorSource) -> str:
    """查询 + 格式化各挂载点磁盘使用率（%），按使用率从高到低排列。

    如 "磁盘使用率：/ 45.2%，/data 88.1%"；无数据返回"磁盘使用率：暂无数据…"。
    """
    try:
        mounts = monitor.disk_usage()
    except MonitorError as exc:
        logger.warning("查询 磁盘使用率失败: %s", exc)
        return f"查询 磁盘使用率失败：{exc}"

    if not mounts:
        return f"磁盘使用率：{_NO_DATA_HINT}"
    detail = "，".join(f"{m['mountpoint']} {_pct(m['usage'])}" for m in mounts)
    return f"磁盘使用率：{detail}"


def active_alerts_text(monitor: MonitorSource, *, with_freshness: bool = False) -> str:
    """查询 + 格式化当前 pending / firing 告警，逐条列名称/级别/实例/状态。

    无活动告警返回 "当前没有 pending / firing 的告警（所有规则均 inactive）"。
    """
    try:
        alerts = monitor.active_alerts()
    except MonitorError as exc:
        logger.warning("查询 告警状态失败: %s", exc)
        text = f"查询 告警状态失败：{exc}"
        return _append_freshness(text, source="Prometheus active alerts", enabled=with_freshness)

    if not alerts:
        text = "当前没有 pending / firing 的告警（所有规则均 inactive）"
        return _append_freshness(text, source="Prometheus active alerts", enabled=with_freshness)

    firing = sum(1 for a in alerts if a["state"] == "firing")
    pending = sum(1 for a in alerts if a["state"] == "pending")
    lines = [f"当前告警（firing {firing} 条 / pending {pending} 条）："]
    for a in alerts:
        state = "▲ firing" if a["state"] == "firing" else "▷ pending"
        line = f"  [{state}] {a['alertname']} severity={a['severity']} instance={a['instance']}"
        if a.get("value") is not None:
            line += f"（当前值约 {a['value']:.1f}）"
        if a.get("summary"):
            line += f"：{a['summary']}"
        lines.append(line)
    text = "\n".join(lines)
    return _append_freshness(text, source="Prometheus active alerts", enabled=with_freshness)


def metric_text(
    monitor: MonitorSource,
    definition: MetricDefinition,
    *,
    with_freshness: bool = False,
) -> str:
    """统一指标目录的查询与格式化入口。"""
    if definition.kind == "alert_count":
        return active_alerts_text(monitor, with_freshness=with_freshness)
    try:
        samples = monitor.query_metric(definition.metric_id)
    except MonitorError as exc:
        logger.warning("查询 %s 失败: %s", definition.display_name, exc)
        text = f"查询 {definition.display_name}失败：{exc}"
        return _append_freshness(text, source=definition.metric_id, enabled=with_freshness)
    if not samples:
        text = f"{definition.display_name}：{_NO_DATA_HINT}"
        return _append_freshness(text, source=definition.metric_id, enabled=with_freshness)

    def fmt(value: float) -> str:
        if definition.unit == "%":
            return f"{value:.1f}%"
        if definition.unit == "bytes":
            return f"{value:.0f} B"
        if definition.unit == "MiB/s":
            return f"{value:.2f} MiB/s"
        if definition.unit == "errors/s":
            return f"{value:.3f} errors/s"
        if definition.unit == "load/core":
            return f"{value:.3f} load/core"
        return f"{value:g} {definition.unit}".rstrip()

    observed_at = epoch_to_iso(max((sample.timestamp or 0 for sample in samples), default=0) or None)
    if len(samples) == 1 and set(samples[0].labels).issubset({"instance"}):
        text = f"{definition.display_name}：{fmt(samples[0].value)}"
        return _append_freshness(
            text,
            source=definition.metric_id,
            observed_at=observed_at,
            enabled=with_freshness,
        )

    details: list[str] = []
    for sample in samples:
        label = ", ".join(f"{key}={value}" for key, value in sample.labels.items()) or "value"
        details.append(f"{label} {fmt(sample.value)}")
    text = f"{definition.display_name}：" + "；".join(details)
    return _append_freshness(
        text,
        source=definition.metric_id,
        observed_at=observed_at,
        enabled=with_freshness,
    )


def _append_freshness(
    text: str,
    *,
    source: str,
    observed_at: str | None = None,
    enabled: bool,
) -> str:
    if not enabled:
        return text
    return text + "\n" + freshness_note(source=source, observed_at=observed_at)
