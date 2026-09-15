"""AlertAnalyzer / build_alert_context：上下文组装边界 + 批量分析的短路语义（离线）。

背景（阶段 8）：analysis.py 的正常路径已被 test_chat 的告警链路**间接**覆盖，
缺的是边界——实例信息缺失、趋势缺数据、LLM 未配置时的短路。这些分支写错会静默失效：
告警照常记录、只是分析结果悄悄变空，没人会发现。
"""
from __future__ import annotations

from src.core.analysis import AlertAnalyzer, build_alert_context
from src.interfaces.metric import MetricRuntimeConfig, MetricSurfaceConfig, MetricsConfig
from src.interfaces.monitor import MonitorError

from tests.conftest import FakeLLM, FakeMonitor, make_alert


class _EmptyTrendMonitor(FakeMonitor):
    """监控源对任何实例都返回空趋势（模拟 Prometheus 不可达）。"""

    def query_metric_trend(self, metric_id, instance, *, minutes=60, step="5m", selector=None):
        return {}


# ---------------------------------------------------------------------------
# build_alert_context：实例信息缺失分支
# ---------------------------------------------------------------------------
def test_context_missing_instance_skips_trend_query():
    """instance 为 unknown / 空 → 走「实例信息缺失」分支，根本不去查趋势。"""
    for missing in ("unknown", ""):
        ctx = build_alert_context(make_alert("A", instance=missing), FakeMonitor())
        assert "实例信息缺失，无法查询历史趋势" in ctx
        assert "近 60 分钟趋势（node_exporter）" not in ctx


def test_context_with_instance_queries_trend():
    """正常 instance → 查询趋势并给出带数值的行。"""
    ctx = build_alert_context(make_alert("A"), FakeMonitor())
    assert "近 60 分钟趋势（node_exporter）" in ctx
    # FakeMonitor：CPU 有数据、内存无数据（模拟某项指标未采集）
    assert "CPU使用率近 60 分钟趋势：当前 85.0%" in ctx
    assert "内存使用率近 60 分钟趋势：暂无趋势数据" in ctx


def test_context_fills_placeholders_for_missing_metrics():
    """监控源一个指标都不返回 → 补三行「暂无趋势数据」占位（而非静默留白）。"""
    ctx = build_alert_context(make_alert("A"), _EmptyTrendMonitor())
    for title in ("CPU使用率", "内存使用率", "磁盘使用率"):
        assert f"- {title}近 60 分钟趋势：暂无趋势数据（Prometheus 不可达或无 Node Exporter）" in ctx


def test_context_trend_error_degrades_not_raises():
    """趋势取数抛 MonitorError → 记日志并降级为占位，不让整个上下文失败。

    「绝不整体抛错」是本模块的显式承诺：即使完全没有趋势数据，也要把告警详情交给模型。
    """

    class BoomTrend(FakeMonitor):
        def query_metric_trend(self, metric_id, instance, *, minutes=60, step="5m", selector=None):
            raise MonitorError("Prometheus 连接失败（模拟）")

    ctx = build_alert_context(make_alert("A"), BoomTrend())
    assert "告警名称：A" in ctx  # 告警详情仍在
    assert "暂无趋势数据" in ctx  # 趋势降级


def test_context_includes_optional_annotations():
    """summary / description 有则补行，无则不出现空行。"""
    alert = make_alert("A", summary="CPU 持续偏高")
    alert.annotations["description"] = "持续 5 分钟超过 85%"
    ctx = build_alert_context(alert, FakeMonitor())
    assert "摘要：CPU 持续偏高" in ctx
    assert "描述：持续 5 分钟超过 85%" in ctx


def test_context_trend_minutes_is_honored():
    """trend_minutes 入参必须真的传下去并体现在文案里。"""
    ctx = build_alert_context(make_alert("A"), FakeMonitor(), trend_minutes=15)
    assert "近 15 分钟趋势" in ctx
    assert "近 60 分钟趋势" not in ctx


def test_context_respects_alert_analysis_metric_limit():
    config = MetricsConfig(
        max_alert_analysis_metrics=1,
        metrics={
            "cpu.usage": MetricRuntimeConfig(
                metric_id="cpu.usage",
                surfaces=MetricSurfaceConfig(alert_analysis=True),
            ),
            "memory.usage": MetricRuntimeConfig(
                metric_id="memory.usage",
                surfaces=MetricSurfaceConfig(alert_analysis=True),
            ),
        },
    )

    ctx = build_alert_context(make_alert("A"), FakeMonitor(), metrics_config=config)

    assert "CPU使用率近" in ctx
    assert "内存使用率近" not in ctx


# ---------------------------------------------------------------------------
# AlertAnalyzer：批量分析的短路语义
# ---------------------------------------------------------------------------
def test_analyze_raises_when_llm_not_configured(fakemonitor):
    """单条 analyze：LLM 未配置 → 抛异常（由 analyze_many 收敛）。"""
    import pytest

    with pytest.raises(Exception, match="LLM 未配置"):
        AlertAnalyzer(fakemonitor, llm=None).analyze(make_alert("A"))


def test_analyze_many_empty_when_llm_not_configured(fakemonitor):
    """LLM 未配置 → 整批返回空列表，告警接收照常完成（不阻塞）。"""
    analyzer = AlertAnalyzer(fakemonitor, llm=None)
    assert analyzer.analyze_many([make_alert("A"), make_alert("B")]) == []


def test_analyze_many_continues_after_single_failure(fakemonitor):
    """单条分析失败仅跳过该条，不影响后续（与「无 LLM 整批短路」区分开）。"""

    class FlakyLLM(FakeLLM):
        def __init__(self):
            super().__init__()
            self.n = 0

        def complete(self, prompt, *, system=None):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("网络抖动（模拟）")
            return super().complete(prompt, system=system)

    analyzer = AlertAnalyzer(fakemonitor, llm=FlakyLLM())
    entries = analyzer.analyze_many([make_alert("A"), make_alert("B")])
    assert len(entries) == 1
    assert entries[0]["alertname"] == "B"  # 失败的是 A，B 照常分析


def test_analyze_many_returns_entry_fields(fakemonitor, fakellm):
    """成功条目字段齐全（api 层与存储依赖这些键）。

    `alert_key` 是 M4 追加的归档关联键（见 storage/models.py::Alert.alert_key）。
    """
    entries = AlertAnalyzer(fakemonitor, llm=fakellm).analyze_many([make_alert("A")])
    (entry,) = entries
    assert set(entry) == {
        "alert_key", "alertname", "severity", "instance", "advice", "analyzed_at"
    }
    assert entry["alertname"] == "A"
    assert entry["advice"] == fakellm.text


def test_analyze_passes_system_prompt_from_core_prompts(fakemonitor, fakellm):
    """system 提示词默认取 src/core/prompts（阶段 4 下沉后 core 不得反向依赖 agent）。"""
    from src.core.prompts import ALERT_ANALYSIS_SYSTEM

    AlertAnalyzer(fakemonitor, llm=fakellm).analyze(make_alert("A"))
    _prompt, system = fakellm.calls[0]
    assert system == ALERT_ANALYSIS_SYSTEM


def test_analyze_custom_system_overrides_default(fakemonitor, fakellm):
    """显式传入 system 时优先于默认（构造参数生效）。"""
    AlertAnalyzer(fakemonitor, llm=fakellm, system="自定义 system").analyze(make_alert("A"))
    assert fakellm.calls[0][1] == "自定义 system"
