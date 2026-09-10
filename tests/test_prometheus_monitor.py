"""PrometheusMonitor：解析边界与降级（全程离线，monkeypatch 掉 _request）。

背景（阶段 8）：本插件解析 Prometheus 返回值的地方是全项目「坏数据」最密集处——
越界百分比、空序列、单条 metric 缺 value、告警 value 非数值。
这些分支写错不会崩，只会把脏数据悄悄当成真实读数报给用户 / 喂给 LLM。

离线手法：monkeypatch PrometheusMonitor._request，不发起任何 HTTP 请求。
"""
from __future__ import annotations

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
