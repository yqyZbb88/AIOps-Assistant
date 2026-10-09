"""17 项即时/趋势查询离线验收矩阵；真实表达式由 promtool 隔离验收。"""
import pytest
from src.interfaces.host import MonitorTarget
from src.interfaces.monitor import MonitorError, MetricNotApplicable
from src.metrics.catalog import METRIC_CATALOG
from src.plugins.monitors.prometheus_monitor import PrometheusMonitor

TARGET = MonitorTarget(job="node", instance="fixture:9100")


@pytest.mark.parametrize("definition", METRIC_CATALOG, ids=lambda d: d.metric_id)
def test_every_metric_instant_and_trend_contract(definition, monkeypatch):
    monitor = PrometheusMonitor(base_url="http://offline.invalid", timeout=1)
    queries = []
    def request(self, path, params):
        queries.append((path, params))
        if path == "query":
            return {"result": [{"metric": {"device": "a"}, "value": [100, "0"]}, {"metric": {"device": "b"}, "value": [100, "2"]}]}
        return {"result": [{"metric": {"device": "a"}, "values": [[100, "0"], [115, "0"]]}, {"metric": {"device": "b"}, "values": [[100, "1"], [115, "2"]]}]}
    monkeypatch.setattr(PrometheusMonitor, "_request", request)
    samples = monitor.query_metric(definition.metric_id, target=TARGET)
    trend = monitor.query_metric_trend(definition.metric_id, target=TARGET)
    assert [sample.value for sample in samples] == [0, 2]
    assert trend["state"] == "ok" and trend["points"] == 2
    assert trend["series"][0]["values"] == [[100, 0], [115, 0]]
    assert trend["series"][1]["labels"]["device"] == "b"
    assert trend["step_seconds"] == 15
    assert {path for path, _ in queries} == {"query", "query_range"}
    for _, params in queries:
        expr = params["query"]
        assert "__SELECTOR" not in expr and 'job="node"' in expr and 'instance="fixture:9100"' in expr
    if definition.metric_id == "disk.usage":
        assert "avg by" not in queries[-1][1]["query"]
    if definition.kind == "alert_count":
        assert "ALERTS" in queries[-1][1]["query"] and "max(up{" in queries[-1][1]["query"]
        assert '"pending"' in queries[-1][1]["query"] and '"firing"' in queries[-1][1]["query"]


@pytest.mark.parametrize("definition", METRIC_CATALOG, ids=lambda d: d.metric_id)
def test_every_metric_empty_and_failures_are_distinct(definition, monkeypatch):
    monitor = PrometheusMonitor(base_url="http://offline.invalid", timeout=1)
    monkeypatch.setattr(monitor, "_query", lambda expr: [])
    monkeypatch.setattr(monitor, "_query_range", lambda *a, **k: [])
    assert monitor.query_metric(definition.metric_id, target=TARGET) == []
    assert monitor.query_metric_trend(definition.metric_id, target=TARGET)["state"] == "no_data"
    def failed(*a, **k):
        raise MonitorError("offline failure")
    monkeypatch.setattr(monitor, "_query", failed)
    monkeypatch.setattr(monitor, "_query_range", failed)
    with pytest.raises(MonitorError):
        monitor.query_metric(definition.metric_id, target=TARGET)
    with pytest.raises(MonitorError):
        monitor.query_metric_trend(definition.metric_id, target=TARGET)


def test_swap_not_applicable_and_invalid_data_are_distinct(monkeypatch):
    monitor = PrometheusMonitor(base_url="http://offline.invalid", timeout=1)
    monkeypatch.setattr(monitor, "_query", lambda expr: [{"metric": {}, "value": [100, "-1"]}])
    with pytest.raises(MetricNotApplicable):
        monitor.query_metric("memory.swap_usage", target=TARGET)
    monkeypatch.setattr(monitor, "_query_range", lambda *a, **k: [{"values": [[100, "-1"]]}])
    assert monitor.query_metric_trend("memory.swap_usage", target=TARGET)["state"] == "not_applicable"
    monkeypatch.setattr(monitor, "_query_range", lambda *a, **k: [{"values": [[100, "NaN"]]}])
    assert monitor.query_metric_trend("memory.swap_usage", target=TARGET)["state"] == "no_data"


def test_range_limits_null_gaps_and_insufficient_samples(monkeypatch):
    monitor = PrometheusMonitor(base_url="http://offline.invalid", timeout=1)
    calls = []
    def ranges(expr, minutes, step):
        calls.append(step)
        return [{"metric": {"device": f"d{i:02}"}, "values": [[t * 360, "NaN" if t == 10 else str(t)] for t in range(300)]} for i in range(40)]
    monkeypatch.setattr(monitor, "_query_range", ranges)
    trend = monitor.query_metric_trend("network.rx_bytes", target=TARGET, minutes=1440, step="1s")
    assert calls == ["360s"] and trend["truncated"] and trend["series_total"] == 40
    assert len(trend["series"]) == 32 and len(trend["series"][0]["values"]) == 241
    assert trend["series"][0]["values"][10][1] is None
    monkeypatch.setattr(monitor, "_query_range", lambda *a, **k: [{"values": [[100, "0"]]}])
    one = monitor.query_metric_trend("network.rx_bytes", target=TARGET)
    assert one["points"] == 1 and one["latest"] == 0
