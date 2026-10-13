"""只验收固定本机测试端口的独立 Prometheus；不连接业务目标或修改业务配置。"""
import argparse
import json
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".runtime/console-improvements/prometheus-live"
BASE = "http://127.0.0.1:19069"


def query(path, **params):
    with urlopen(BASE + "/api/v1/" + path + ("?" + urlencode(params) if params else ""), timeout=5) as response:
        data = json.load(response)
    assert data["status"] == "success", data
    return data["data"]


def wait_for(predicate):
    for _ in range(40):
        try:
            if predicate():
                return
        except OSError:
            pass
        time.sleep(.5)
    raise AssertionError("隔离 Prometheus 未在 20 秒内完成发现/抓取")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / "targets.json"
    if args.prepare:
        # 仅测试使用 1 秒发现/抓取；生产模板仍为 30 秒发现和 15 秒抓取。
        (OUT / "prometheus.yml").write_text('global:\n  scrape_interval: 1s\nscrape_configs:\n  - job_name: aiops-hosts\n    file_sd_configs:\n      - files: [/fixtures/targets.json]\n        refresh_interval: 1s\n', encoding="utf-8")
        target.write_text('[{"targets":["127.0.0.1:9090"]}]', encoding="utf-8")
        return
    target_expr = 'up{job="aiops-hosts",instance="127.0.0.1:9090"}'
    wait_for(lambda: bool(query("query", query=target_expr)["result"]) and query("query", query=target_expr)["result"][0]["value"][1] == "1")
    targets = query("targets", state="active")["activeTargets"]
    assert len(targets) == 1 and targets[0]["labels"]["job"] == "aiops-hosts"
    dashboard = json.loads((ROOT / "deploy/grafana/dashboards/host-metrics.json").read_text(encoding="utf-8"))
    checks = []
    for panel in dashboard["panels"]:
        expr = panel["targets"][0]["expr"].replace("$job", "aiops-hosts").replace("$instance", "127.0.0.1:9090")
        now = time.time()
        instant = query("query", query=expr)
        trend = query("query_range", query=expr, start=now - 300, end=now, step="15s")
        checks.append({"metric": panel["title"], "instant_series": len(instant["result"]), "trend_series": len(trend["result"])})
    assert len(checks) == 17
    # 自身抓取没有 node_*，验证真引擎返回空数据而不伪造零；活动告警由 up=1 门控补零。
    assert all(item["instant_series"] == 0 for item in checks[:-1])
    assert checks[-1]["instant_series"] == 2
    target.write_text("[]", encoding="utf-8")
    wait_for(lambda: not query("targets", state="active")["activeTargets"])
    wait_for(lambda: not query("query", query=target_expr)["result"])
    result = {"checks": checks, "target_labels_verified": True, "file_sd_removal_verified": True,
              "note": "指标有效数值由 promtool 合成测试覆盖；本项验证实时 HTTP、无 node 采集器和 file_sd 移除"}
    (OUT.parent / "prometheus-live-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print("17 项即时/趋势 HTTP 查询、精确标签、缺采集器无数据、file_sd 移除：通过")


if __name__ == "__main__":
    main()
