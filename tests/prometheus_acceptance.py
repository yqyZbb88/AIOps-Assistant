"""生成 promtool 的真实 PromQL 隔离验收；只使用合成标签和样本。

python tests/prometheus_acceptance.py <output.yml>
然后使用与部署相同版本的 promtool test rules <output.yml>。
不加载应用、模型、SSH 或用户数据库。
"""
from pathlib import Path
import sys
import yaml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.interfaces.host import MonitorTarget
from src.metrics.catalog import METRIC_CATALOG
from src.plugins.monitors.prometheus_monitor import PrometheusMonitor


def fixture():
    monitor = PrometheusMonitor(base_url="http://unused.invalid", timeout=1)
    target = MonitorTarget(job="node", instance="fixture:9100")
    labels = 'job="node",instance="fixture:9100"'
    inputs = []
    def add(name, values, extra=""):
        inputs.append({"series": name + "{" + labels + ("," + extra if extra else "") + "}", "values": values})
    for cpu in ("0", "1"):
        add("node_cpu_seconds_total", "0+12x40", 'cpu="' + cpu + '",mode="idle"')
        add("node_cpu_seconds_total", "0+1.5x40", 'cpu="' + cpu + '",mode="iowait"')
    for name, value in {"up": 1, "node_load1": 2, "node_memory_MemAvailable_bytes": 400, "node_memory_MemTotal_bytes": 1000,
                        "node_memory_SwapFree_bytes": 0, "node_memory_SwapTotal_bytes": 0,
                        "node_procs_running": 3, "node_procs_blocked": 2, "node_boot_time_seconds": 0}.items():
        add(name, str(value) + "+0x40")
    for device, mount, free in (("sda", "/", 40), ("sdb", "/data", 10)):
        fs = 'device="' + device + '",mountpoint="' + mount + '",fstype="ext4"'
        add("node_filesystem_avail_bytes", str(free) + "+0x40", fs)
        add("node_filesystem_size_bytes", "100+0x40", fs)
        add("node_disk_read_bytes_total", "0+15728640x40", 'device="' + device + '"')
        add("node_disk_written_bytes_total", "0+31457280x40", 'device="' + device + '"')
        add("node_disk_io_time_seconds_total", "0+3x40", 'device="' + device + '"')
    for device in ("eth0", "eth1"):
        for name, step in {"node_network_receive_bytes_total": 15728640, "node_network_transmit_bytes_total": 31457280,
                           "node_network_receive_errs_total": 0, "node_network_transmit_errs_total": 0}.items():
            add(name, "0+" + str(step) + "x40", 'device="' + device + '"')
    add("ALERTS", "1+0x40", 'alertname="SyntheticFiring",alertstate="firing"')
    add("ALERTS", "1+0x40", 'alertname="SyntheticPending",alertstate="pending"')

    def sample(extra, value, name=""):
        return {"labels": name + "{" + labels + ("," + extra if extra else "") + "}", "value": value}
    expected = {
        "cpu.usage": [{"labels": '{instance="fixture:9100"}', "value": 20}],
        "cpu.load1": [{"labels": '{instance="fixture:9100"}', "value": 1}],
        "cpu.iowait": [{"labels": '{instance="fixture:9100"}', "value": 10}],
        "memory.usage": [sample("", 60)],
        "memory.available": [sample("", 400, "node_memory_MemAvailable_bytes")],
        "memory.swap_usage": [sample("", -1)],
        "disk.usage": [sample('device="sda",mountpoint="/",fstype="ext4"', 60), sample('device="sdb",mountpoint="/data",fstype="ext4"', 90)],
        "system.process_count": [{"labels": '{instance="fixture:9100"}', "value": 3}], "system.uptime": [sample("", 300)],
        "alerts.active_count": [{"labels": '{state="firing"}', "value": 1}, {"labels": '{state="pending"}', "value": 1}],
    }
    for metric, value in (("disk.read_bytes", 1), ("disk.write_bytes", 2), ("disk.io_util", 20),
                          ("network.rx_bytes", 1), ("network.tx_bytes", 2), ("network.rx_errors", 0), ("network.tx_errors", 0)):
        expected[metric] = [{"labels": '{instance="fixture:9100",device="' + device + '"}', "value": value} for device in (("sda", "sdb") if metric.startswith("disk.") else ("eth0", "eth1"))]
    expr_tests = []
    for metric in METRIC_CATALOG:
        for range_query in (False, True):
            expr = monitor._render_expr(metric, target=target, range_query=range_query or metric.kind == "alert_count")
            expr_tests.append({"expr": expr, "eval_time": "5m", "exp_samples": expected[metric.metric_id]})
    tests = [{"name": "all-17-instant-and-trend-expressions", "interval": "15s", "input_series": inputs, "promql_expr_test": expr_tests}]
    alert_expr = monitor._render_expr(monitor._definition("alerts.active_count"), target=target, range_query=True)
    for health, samples in ((1, [{"labels": '{state="firing"}', "value": 0}, {"labels": '{state="pending"}', "value": 0}]), (0, [])):
        tests.append({"name": "no-alerts-up-" + str(health), "interval": "15s",
                      "input_series": [{"series": "up{" + labels + "}", "values": str(health) + "+0x40"}],
                      "promql_expr_test": [{"expr": alert_expr, "eval_time": "5m", "exp_samples": samples}]})
    tests.append({"name": "missing-collector-and-label-mismatch", "interval": "15s", "input_series": [{"series": 'node_disk_read_bytes_total{job="other",instance="other:9100",device="sda"}', "values": "0+1x40"}],
                  "promql_expr_test": [{"expr": monitor._render_expr(metric, target=target, range_query=True), "eval_time": "5m", "exp_samples": []}
                                      for metric in METRIC_CATALOG]})
    tests.append({"name": "one-counter-sample-is-insufficient-for-rate", "interval": "15s", "input_series": [{"series": "node_disk_read_bytes_total{" + labels + ',device="sda"}', "values": "0"}],
                  "promql_expr_test": [{"expr": monitor._render_expr(monitor._definition("disk.read_bytes"), target=target, range_query=True), "eval_time": "0s", "exp_samples": []}]})
    return {"rule_files": [], "evaluation_interval": "15s", "fuzzy_compare": True, "tests": tests}


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(yaml.safe_dump(fixture(), allow_unicode=True), encoding="utf-8")
    print("Generated isolated acceptance: 17 metrics, instant/trend, multi-device, zero, missing labels, insufficient samples")
