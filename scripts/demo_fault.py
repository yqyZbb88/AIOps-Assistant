"""安全演示指标开关：只写 Node Exporter textfile，不消耗真实资源。"""
from __future__ import annotations

import argparse
from pathlib import Path

TARGET_NAME = "aiops_demo_fault.prom"
METRICS = {"cpu": "aiops_demo_cpu_percent", "memory": "aiops_demo_memory_percent", "disk": "aiops_demo_disk_percent"}
HIGH = {"cpu": 95, "memory": 96, "disk": 96}


def write(values: dict[str, float], target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, metric in METRICS.items():
        lines.extend((f"# HELP {metric} Safe synthetic {name} fault gauge.", f"# TYPE {metric} gauge", f"{metric} {values[name]}"))
    temporary = target.with_suffix(".tmp")
    # Node Exporter 的文本格式要求 LF；Windows 默认 CRLF 会把类型解析成 ``gauge\r``。
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
    temporary.replace(target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fault", choices=[*METRICS, "all", "reset"])
    parser.add_argument("state", nargs="?", choices=["on", "off"], default="on")
    parser.add_argument("--runtime", type=Path, default=Path("/runtime"), help="容器部署目录，默认 /runtime")
    args = parser.parse_args()
    target = args.runtime / "textfile" / TARGET_NAME
    current = {name: 0.0 for name in METRICS}
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            for name, metric in METRICS.items():
                if len(parts) == 2 and parts[0] == metric:
                    current[name] = float(parts[1])
    targets = list(METRICS) if args.fault in {"all", "reset"} else [args.fault]
    enabled = args.state == "on" and args.fault != "reset"
    for name in targets:
        current[name] = float(HIGH[name] if enabled else 0)
    write(current, target)
    print("演示指标已更新：" + ", ".join(f"{name}={current[name]:g}" for name in METRICS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
