"""告警 AI 分析：上下文组装 + LLM 编排。

承接原 ops_bot/context.py 的 alert_context_text（趋势取数已下沉到 MonitorSource。
instance_trend）与 ops_bot/assistant.py 的 analyze_alert 编排，改为走 LLMClient 接口：
- build_alert_context：告警详情 + 实例近 N 分钟趋势 → 纯文本（尽力而为，永不抛错）；
- AlertAnalyzer：注入 monitor + llm，analyze_many 对多条无规则告警逐个分析，
  失败 / LLM 未配置仅记日志，绝不阻塞告警接收。

「AI 分析」的终端打印由调用方（core/agent.py）负责，本模块保持纯净可测。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.core.prompts import ALERT_ANALYSIS_SYSTEM
from src.interfaces.llm_client import LLMClient
from src.interfaces.metric import MetricDefinition, MetricsConfig
from src.interfaces.monitor import MonitorSource
from src.plugins.monitors.prometheus_catalog import METRIC_CATALOG
from src.utils.metrics_config import default_metrics_config
from src.storage.models import Alert

logger = logging.getLogger("ops_agent.analysis")

_UNAVAILABLE = "暂无趋势数据（Prometheus 不可达或无 Node Exporter）"

# 给 LLM 的分析提问（与旧 ops_bot/assistant.py 的 _ALERT_ASK_TPL 语义一致）
_ANALYSIS_QUESTION = "请分析下面这条告警，给出根因假设与处理建议："


# ---------------------------------------------------------------------------
# 上下文纯组装
# ---------------------------------------------------------------------------
def _fmt_value(value: float | None, unit: str) -> str:
    if value is None:
        return "未知"
    if unit == "%":
        return f"{value:.1f}%"
    if unit == "bytes":
        return f"{value:.0f} B"
    if unit == "MiB/s":
        return f"{value:.2f} MiB/s"
    if unit == "errors/s":
        return f"{value:.3f} errors/s"
    if unit == "load/core":
        return f"{value:.3f} load/core"
    return f"{value:g} {unit}".rstrip()


def build_alert_context(
    alert: Alert,
    monitor: MonitorSource,
    *,
    trend_minutes: int = 60,
    metrics_config: MetricsConfig | None = None,
    catalog: tuple[MetricDefinition, ...] = METRIC_CATALOG,
) -> str:
    """组装一条告警的 LLM 分析上下文：告警详情 + 实例近 trend_minutes 分钟趋势。

    只做「取数 + 拼文本」，不做任何决策；Prometheus 不可达 / 某指标缺失时
    对应行标注不可用，绝不整体抛错（即使完全没有趋势数据也能给模型告警详情）。
    """
    lines = [
        "[告警详情]",
        f"- 告警名称：{alert.alertname}",
        f"- 严重级别：{alert.severity}",
        f"- 实例：{alert.instance}",
        f"- 状态：{alert.status}",
        f"- 开始时间：{alert.startsAt or '未知'}",
    ]
    summary = alert.annotations.get("summary")
    description = alert.annotations.get("description")
    if summary:
        lines.append(f"- 摘要：{summary}")
    if description:
        lines.append(f"- 描述：{description}")

    instance = alert.instance
    if not instance or instance == "unknown":
        lines.append("[实例趋势]\n- 实例信息缺失，无法查询历史趋势")
        return "\n".join(lines)

    config = metrics_config or default_metrics_config()
    definitions = {definition.metric_id: definition for definition in catalog}
    selected = config.selected("alert_analysis")[: config.max_alert_analysis_metrics]
    lines.append(f"[实例 {instance} 近 {trend_minutes} 分钟趋势（node_exporter）]")
    for runtime in selected:
        definition = definitions.get(runtime.metric_id)
        if definition is None:
            continue
        title = definition.display_name.replace(" ", "")
        try:
            metric = monitor.query_metric_trend(
                definition.metric_id,
                instance,
                minutes=trend_minutes,
            )
        except Exception as exc:  # 尽力而为：单项失败不影响整体
            logger.debug("告警趋势取数失败 metric=%s instance=%s: %s", definition.metric_id, instance, exc)
            metric = {}
        lines.append(_trend_line(title, metric, trend_minutes=trend_minutes, unit=definition.unit))

    return "\n".join(lines)


def _trend_line(
    title: str,
    summary: dict[str, Any],
    *,
    trend_minutes: int,
    unit: str,
) -> str:
    """把单指标趋势汇总拼成一行文本。"""
    if not summary or not summary.get("points"):
        return f"- {title}近 {trend_minutes} 分钟趋势：{_UNAVAILABLE}"
    return (
        f"- {title}近 {trend_minutes} 分钟趋势：当前 {_fmt_value(summary['latest'], unit)} "
        f"（最低 {_fmt_value(summary['min'], unit)} / 最高 {_fmt_value(summary['max'], unit)}，"
        f"近 {summary['points']} 个采样点）"
    )


# ---------------------------------------------------------------------------
# LLM 编排（注入接口，失败不阻塞）
# ---------------------------------------------------------------------------
class AlertAnalyzer:
    """无自动处理规则的告警 → LLM 根因分析。"""

    def __init__(
        self,
        monitor: MonitorSource,
        llm: LLMClient | None = None,
        *,
        trend_minutes: int = 60,
        system: str | None = None,
        metrics_config: MetricsConfig | None = None,
    ) -> None:
        self.monitor = monitor
        self.llm = llm
        self.trend_minutes = trend_minutes
        self.system = system or ALERT_ANALYSIS_SYSTEM
        self.metrics_config = metrics_config or default_metrics_config()

    def analyze(self, alert: Alert) -> dict[str, Any]:
        """对单条告警生成 AI 建议条目；失败（未配置 / 网络 / 空返回）抛 LLMError。"""
        if self.llm is None:
            raise _LLMUnavailable("LLM 未配置（settings.llm.enabled=false 或无 provider）")

        context = build_alert_context(
            alert,
            self.monitor,
            trend_minutes=self.trend_minutes,
            metrics_config=self.metrics_config,
        )
        prompt = f"{_ANALYSIS_QUESTION}\n\n{context}"
        advice = self.llm.complete(prompt, system=self.system)
        if len(advice) > 8000:
            advice = advice[:8000] + "\n…（AI 建议过长已截断）"

        return {
            "alert_key": alert.alert_key,  # 与归档表的关联键（见 storage/models.py）
            "alertname": alert.alertname,
            "severity": alert.severity,
            "instance": alert.instance,
            "advice": advice,
            "analyzed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def analyze_many(self, alerts: list[Alert]) -> list[dict[str, Any]]:
        """对多条无规则告警逐个分析；单条失败仅记日志跳过，不阻塞整体。

        LLM 未配置 / 不可用时整批返回空列表（告警接收照常完成）。
        """
        entries: list[dict[str, Any]] = []
        for alert in alerts:
            try:
                entries.append(self.analyze(alert))
            except _LLMUnavailable as exc:
                logger.warning("跳过告警 AI 分析 alertname=%s: %s", alert.alertname, exc)
                return entries  # 无 LLM 时后续各条结果相同，直接停
            except Exception as exc:  # LLMError / 网络等统一收敛，不影响告警记录
                logger.warning("告警 LLM 分析失败 alertname=%s: %s", alert.alertname, exc)
                continue
        return entries


class _LLMUnavailable(Exception):
    """内部标记：LLM 未配置（区别于调用失败，用于整批短路）。"""
