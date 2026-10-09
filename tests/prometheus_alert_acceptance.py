"""Generate isolated promtool regression tests for host alert labels.

Run: python tests/prometheus_alert_acceptance.py <output.yml>
Mount deploy/prometheus/alerts.yml at /etc/prometheus/alerts.yml for promtool.
Uses synthetic samples only; does not load the application or send notifications.
"""
from pathlib import Path
import sys

import yaml


ALERTS = ("HighCPUUsage", "HighMemoryUsage", "HighDiskUsage")
INSTANCE = "fixture:9100"


def native_series(job="node", healthy=False):
    labels = f'job="{job}",instance="{INSTANCE}"'
    values = {
        "node_cpu_seconds_total": ("0+12x40" if healthy else "0+1.5x40", ',cpu="0",mode="idle"'),
        "node_memory_MemAvailable_bytes": ("60+0x40" if healthy else "10+0x40", ""),
        "node_memory_MemTotal_bytes": ("100+0x40", ""),
        "node_filesystem_avail_bytes": ("60+0x40" if healthy else "10+0x40", ',device="sda",mountpoint="/",fstype="ext4"'),
        "node_filesystem_size_bytes": ("100+0x40", ',device="sda",mountpoint="/",fstype="ext4"'),
    }
    return [{"series": name + "{" + labels + extra + "}", "values": value}
            for name, (value, extra) in values.items()]


def demo_series(job="node-exporter"):
    return [{"series": f'aiops_demo_{kind}_percent{{job="{job}",instance="{INSTANCE}"}}',
             "values": "95+0x40"} for kind in ("cpu", "memory", "disk")]


def samples(jobs, state):
    return [{"labels": f'ALERTS{{alertname="{name}",alertstate="{state}",job="{job}",instance="{INSTANCE}",severity="warning"}}',
             "value": 1} for job in jobs for name in ALERTS]


def fixture():
    tests = []
    for name, inputs, jobs in (
        ("native-labels", native_series(), ("node",)),
        ("demo-labels", demo_series(), ("node-exporter",)),
        ("same-job-native-and-demo-deduplicate", native_series() + demo_series("node"), ("node",)),
        ("same-instance-different-jobs-stay-separate", native_series() + demo_series(), ("node", "node-exporter")),
        ("healthy-no-alerts", native_series(healthy=True), ()),
    ):
        assertions = [{"expr": 'ALERTS{alertstate="firing"}', "eval_time": "5m",
                       "exp_samples": samples(jobs, "firing")}]
        if jobs:
            assertions.extend([
                {"expr": 'ALERTS{alertstate="pending"}', "eval_time": "15s",
                 "exp_samples": samples(jobs, "pending")},
                {"expr": 'ALERTS{alertstate="firing"}', "eval_time": "15s",
                 "exp_samples": []},
            ])
        tests.append({"name": name, "interval": "15s",
                      "input_series": inputs, "promql_expr_test": assertions})
    return {"rule_files": ["/etc/prometheus/alerts.yml"],
            "evaluation_interval": "15s", "tests": tests}


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(yaml.safe_dump(fixture()), encoding="utf-8")
    print("Generated host alert acceptance: native/demo, pending/firing, job isolation, deduplication, healthy")
