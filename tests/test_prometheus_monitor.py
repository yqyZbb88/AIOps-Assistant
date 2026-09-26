"""PrometheusMonitor：解析边界与降级（全程离线，monkeypatch 掉 _request）。

覆盖越界百分比、空序列、缺失 value 和非数值告警等输入边界。

离线手法：monkeypatch PrometheusMonitor._request，不发起任何 HTTP 请求。
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.interfaces.monitor import MonitorError
from src.plugins.monitors.prometheus_monitor import (
    PrometheusMonitor,
    _clamp,
    _summarize_values,
)


@pytest.fixture
def monitor():
    return PrometheusMonitor(base_url="http://p:9090", timeout=1)


def _patch_request(monkeypatch, payload):
    """把 _request 换成返回固定 payload 的桩（不联网）。"""
    monkeypatch.setattr(PrometheusMonitor, "_request", lambda self, path, params: payload)


def _patch_query(monkeypatch, result):
    monkeypatch.setattr(PrometheusMonitor, "_query", lambda self, expr: result)


# ---------------------------------------------------------------------------
# _clamp / _summarize_values：纯函数边界
# ---------------------------------------------------------------------------
def test_clamp_bounds():
    """Prometheus 计算误差可能给出 -0.3 或 100.7，必须收敛回 [0,100]。"""
    assert _clamp(150.0) == 100.0
    assert _clamp(-5.0) == 0.0
    assert _clamp(45.5) == 45.5


def test_summarize_empty_returns_points_zero():
    """空序列 → points=0 的三无汇总（调用方据此判「暂无数据」）。"""
    empty = {"latest": None, "min": None, "max": None, "avg": None, "points": 0}
    assert _summarize_values([]) == empty


def test_summarize_skips_none_blank_and_unparsable():
    """None / 空串 / 非数值一律跳过；越界值 clamp 后计入。"""
    out = _summarize_values([[1, None], [2, ""], [3, "bad"], [4, "50"], [5, "150"]])
    assert out["points"] == 2      # 只有 50 与 150 是有效值
    assert out["min"] == 50.0
    assert out["max"] == 100.0     # 150 被 clamp 到 100
    assert out["latest"] == 100.0


def test_summarize_all_invalid_returns_points_zero():
    """全是坏数据 → 等价于无数据，而不是抛错或算出 NaN。"""
    assert _summarize_values([[1, None], [2, "x"]])["points"] == 0


# ---------------------------------------------------------------------------
# cpu_usage / memory_usage：即时查询
# ---------------------------------------------------------------------------
def test_cpu_usage_clamps_out_of_range(monkeypatch, monitor):
    """CPU 越界值（如 100.4）收敛到 100 而非原样返回。"""
    _patch_query(monkeypatch, [{"value": [0, "100.4"]}])
    assert monitor.cpu_usage() == 100.0


def test_cpu_usage_no_series_returns_none(monkeypatch, monitor):
    """空 result（如 up=0 门控命中）→ None，即「暂无数据」。"""
    _patch_query(monkeypatch, [])
    assert monitor.cpu_usage() is None


def test_first_value_malformed_raises_monitor_error(monkeypatch, monitor):
    """序列结构异常（缺 value 字段）→ MonitorError，不静默返回 0。"""
    _patch_query(monkeypatch, [{"metric": {}}])
    with pytest.raises(MonitorError, match="无法解析"):
        monitor.memory_usage()


def test_first_value_non_numeric_raises_monitor_error(monkeypatch, monitor):
    _patch_query(monkeypatch, [{"value": [0, "not-a-number"]}])
    with pytest.raises(MonitorError, match="无法解析"):
        monitor.cpu_usage()


# ---------------------------------------------------------------------------
# disk_usage：排序 + 坏数据跳过
# ---------------------------------------------------------------------------
def test_disk_usage_sorted_desc_and_bad_rows_skipped(monkeypatch, monitor):
    """按使用率降序；单条解析失败只跳过该条，不影响其余挂载点。"""
    _patch_query(
        monkeypatch,
        [
            {"metric": {"mountpoint": "/"}, "value": [0, "45.2"]},
            {"metric": {"mountpoint": "/data"}, "value": [0, "88.1"]},
            {"metric": {"mountpoint": "/broken"}, "value": [0, "oops"]},  # 坏数据
        ],
    )
    mounts = monitor.disk_usage()
    assert [m["mountpoint"] for m in mounts] == ["/data", "/"]  # 降序，坏行已丢
    assert mounts[0]["usage"] == 88.1


def test_disk_usage_missing_mountpoint_uses_placeholder(monkeypatch, monitor):
    """metric 里没有 mountpoint 时给占位名，而不是 KeyError。"""
    _patch_query(monkeypatch, [{"metric": {}, "value": [0, "10"]}])
    assert monitor.disk_usage()[0]["mountpoint"] == "未知挂载点"


# ---------------------------------------------------------------------------
# active_alerts：firing 优先排序 + value 解析
# ---------------------------------------------------------------------------
def test_active_alerts_firing_first_then_by_name(monkeypatch, monitor):
    """排序：firing 全排在 pending 前；同状态按 alertname。"""
    _patch_request(
        monkeypatch,
        {
            "alerts": [
                {"labels": {"alertname": "Zebra"}, "state": "pending"},
                {"labels": {"alertname": "Beta"}, "state": "firing"},
                {"labels": {"alertname": "Alpha"}, "state": "firing"},
            ]
        },
    )
    got = [(a["alertname"], a["state"]) for a in monitor.active_alerts()]
    assert got == [("Alpha", "firing"), ("Beta", "firing"), ("Zebra", "pending")]


def test_active_alerts_value_parsing_and_defaults(monkeypatch, monitor):
    """value 缺失 / 空 / 非数值 → None；labels 缺失 → 占位符，绝不含 None 键。"""
    _patch_request(
        monkeypatch,
        {
            "alerts": [
                {"labels": {"alertname": "A"}, "state": "firing", "value": "92.5"},
                {"labels": {"alertname": "B"}, "state": "pending", "value": ""},
                {"labels": {"alertname": "C"}, "state": "pending", "value": "nan?"},
                {"state": "firing"},  # labels 整个缺失
            ]
        },
    )
    by_name = {a["alertname"]: a for a in monitor.active_alerts()}
    assert by_name["A"]["value"] == 92.5
    assert by_name["B"]["value"] is None
    assert by_name["C"]["value"] is None
    unknown = by_name["unknown"]  # labels 缺失 → 各字段给占位
    assert unknown["severity"] == "none" and unknown["instance"] == "unknown"


# ---------------------------------------------------------------------------
# instance_trend：单指标失败不影响整体
# ---------------------------------------------------------------------------
def test_trend_single_metric_failure_yields_points_zero(monkeypatch, monitor):
    """某指标范围查询抛 MonitorError → 该项给 points=0，其余照常。"""
    calls = {"n": 0}

    def flaky_range(self, expr, minutes, step):
        calls["n"] += 1
        if calls["n"] == 1:
            raise MonitorError("范围查询失败（模拟）")
        return [{"values": [[1, "10"], [2, "20"]]}]

    monkeypatch.setattr(PrometheusMonitor, "_query_range", flaky_range)
    trend = monitor.instance_trend("h:9100")
    titles = ["CPU使用率", "内存使用率", "磁盘使用率"]
    assert trend[titles[0]]["points"] == 0            # 失败的指标降级
    assert trend[titles[1]]["points"] == 2            # 其余正常
    assert trend[titles[1]]["latest"] == 20.0


def test_trend_empty_series_yields_points_zero(monkeypatch, monitor):
    _patch_range(monkeypatch, [])
    assert monitor.instance_trend("h:9100")["CPU使用率"]["points"] == 0


def _patch_range(monkeypatch, series):
    monkeypatch.setattr(
        PrometheusMonitor, "_query_range", lambda self, expr, minutes, step: series
    )


# ---------------------------------------------------------------------------
# _request 的错误收敛（异常 / 非 success / 结构异常）
# ---------------------------------------------------------------------------
def test_request_wraps_network_error_as_monitor_error(monkeypatch, monitor):
    """requests 抛异常 → 统一收敛为 MonitorError（core 只认这个）。"""
    import requests

    def boom(*a, **kw):
        raise requests.RequestException("连不上")

    monkeypatch.setattr(requests, "get", boom)
    with pytest.raises(MonitorError, match="请求失败"):
        monitor.cpu_usage()


def test_request_non_success_status_raises(monkeypatch, monitor):
    """Prometheus 返回 status != success → 带出对方给的 error 文本。"""
    import requests

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "error", "error": "parse error"}

    monkeypatch.setattr(requests, "get", lambda *a, **kw: Resp())
    with pytest.raises(MonitorError, match="parse error"):
        monitor.cpu_usage()


def test_request_non_dict_data_raises(monkeypatch, monitor):
    """data 段不是对象 → 结构异常。"""
    import requests

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "success", "data": ["not", "a", "dict"]}

    monkeypatch.setattr(requests, "get", lambda *a, **kw: Resp())
    with pytest.raises(MonitorError, match="格式异常"):
        monitor.cpu_usage()


def test_base_url_normalized_and_default():
    """base_url 尾部斜杠规整；空值回落 localhost:9090。"""
    assert PrometheusMonitor(base_url="http://x:9090/").base_url == "http://x:9090"
    assert PrometheusMonitor(base_url="").base_url == "http://localhost:9090"


# ---------------------------------------------------------------------------
# 通用指标目录查询
# ---------------------------------------------------------------------------
def test_query_metric_returns_structured_samples(monkeypatch, monitor):
    _patch_query(
        monkeypatch,
        [{"metric": {"instance": "h:9100", "device": "eth0"}, "value": [1, "2.5"]}],
    )

    samples = monitor.query_metric("network.rx_bytes")

    assert samples[0].metric_id == "network.rx_bytes"
    assert samples[0].labels == {"instance": "h:9100", "device": "eth0"}
    assert samples[0].value == 2.5


def test_query_metric_renders_global_instance_and_escapes_labels(monitor):
    definition = monitor._definition("cpu.usage")
    expr = monitor._render_expr(definition, instance='h"1')
    assert 'instance="h\\\"1"' in expr


def test_registered_target_overrides_global_and_metric_instance(monitor):
    from src.interfaces.host import MonitorTarget

    definition = monitor._definition("cpu.usage")
    monitor.metrics_config = replace(monitor.metrics_config, instance="global:9100")
    runtime = monitor.metrics_config.runtime("cpu.usage")
    monitor.metrics_config = replace(
        monitor.metrics_config,
        metrics={
            **monitor.metrics_config.metrics,
            "cpu.usage": replace(
                runtime,
                selector={**runtime.selector, "instance": "metric:9100"},
            ),
        },
    )

    expr = monitor._render_expr(
        definition,
        target=MonitorTarget(job="node-a", instance="a:9100"),
    )

    assert 'job="node-a"' in expr
    assert 'instance="a:9100"' in expr
    assert "global:9100" not in expr and "metric:9100" not in expr


def test_target_status_distinguishes_reachable_down_no_data_and_ambiguous(monitor, monkeypatch):
    from src.interfaces.host import MonitorTarget

    target = MonitorTarget(job="node", instance="a:9100")
    monkeypatch.setattr(
        monitor,
        "_query",
        lambda _expr: [{"metric": {}, "value": [1, "1"]}],
    )
    assert monitor.target_status(target)["state"] == "reachable"

    monkeypatch.setattr(
        monitor,
        "_query",
        lambda _expr: [{"metric": {}, "value": [1, "0"]}],
    )
    assert monitor.target_status(target)["state"] == "unreachable"
    monkeypatch.setattr(monitor, "_query", lambda _expr: [])
    assert monitor.target_status(target)["state"] == "no_data"
    monkeypatch.setattr(
        monitor,
        "_query",
        lambda _expr: [
            {"metric": {}, "value": [1, "1"]},
            {"metric": {}, "value": [1, "1"]},
        ],
    )
    assert monitor.target_status(target)["state"] == "ambiguous"


def test_rendered_promql_has_no_empty_matcher_comma(monitor):
    from dataclasses import replace

    monitor.metrics_config = replace(monitor.metrics_config, instance="h:9100")
    cpu = monitor._render_expr(monitor._definition("cpu.usage"))
    memory = monitor._render_expr(monitor._definition("memory.usage"))
    load = monitor._render_expr(monitor._definition("cpu.load1"))
    disk = monitor._render_expr(monitor._definition("disk.usage"))

    for expr in (cpu, memory, load, disk):
        assert "{," not in expr
    assert '{mode="idle",instance=' in cpu
    assert "{instance=" in memory
    assert '{mode="idle",instance=' in load
    assert '{fstype!~"tmpfs|overlay",instance=' in disk


def test_query_metric_trend_chooses_worst_series(monkeypatch, monitor):
    _patch_range(
        monkeypatch,
        [
            {"values": [[1, "10"], [2, "20"]]},
            {"values": [[1, "60"], [2, "80"]]},
        ],
    )

    trend = monitor.query_metric_trend("cpu.usage", "h:9100")

    assert trend["latest"] == 80.0
    assert trend["max"] == 80.0
    assert trend["points"] == 2


def test_query_metric_unknown_id_is_monitor_error(monitor):
    with pytest.raises(MonitorError, match="未知指标"):
        monitor.query_metric("missing.metric")


def test_alert_count_honors_caller_selector(monkeypatch, monitor):
    """alert_count 分支也必须认 `selector` 里的 instance。

    `query_metric` 的 selector 契约要求 alert_count 按实例过滤。
    """
    _patch_request(monkeypatch, {"alerts": [
        {"labels": {"alertname": "A", "instance": "h1"}, "state": "firing"},
        {"labels": {"alertname": "B", "instance": "h2"}, "state": "firing"},
        {"labels": {"alertname": "C", "instance": "h2"}, "state": "pending"},
        {"labels": {"alertname": "D", "instance": "h3"}, "state": "pending"},
    ]})

    everything = {s.labels["state"]: s.value for s in monitor.query_metric("alerts.active_count")}
    only_h2 = {
        s.labels["state"]: s.value
        for s in monitor.query_metric("alerts.active_count", selector={"instance": "h2"})
    }

    assert everything == {"firing": 2.0, "pending": 2.0}
    assert only_h2 == {"firing": 1.0, "pending": 1.0}


def test_trend_skips_malformed_series(monkeypatch, monitor):
    _patch_range(monkeypatch, [None, {"values": None}, {"values": [[1, "42"], "bad"]}])
    trend = monitor.query_metric_trend("cpu.usage", "h:9100")
    assert trend["points"] == 1
    assert trend["latest"] == 42.0


def test_request_invalid_json_is_monitor_error(monkeypatch, monitor):
    import requests

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            raise ValueError("broken JSON")

    monkeypatch.setattr(requests, "get", lambda *a, **kw: Resp())
    with pytest.raises(MonitorError, match="invalid JSON"):
        monitor.cpu_usage()
