"""跨平台开发监控栈入口；生成本地密钥后调用 Docker Compose。"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / ".runtime"
ENV_FILE = RUNTIME / "compose.env"


def bootstrap() -> dict[str, str]:
    for name in ("assistant-data", "assistant-logs", "secrets", "textfile"):
        (RUNTIME / name).mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists():
        values = {}
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        return values
    user_token = secrets.token_urlsafe(24)
    alert_token = secrets.token_urlsafe(24)
    values = {
        "OPS_API_TOKENS": json.dumps({user_token: "admin", alert_token: "alertmanager"}, separators=(",", ":")),
        "OPS_APPROVER_IDENTITIES": '["admin"]',
        "DEEPSEEK_API_KEY": os.getenv("DEEPSEEK_API_KEY", ""),
        "GF_SECURITY_ADMIN_USER": "admin",
        "GF_SECURITY_ADMIN_PASSWORD": secrets.token_urlsafe(18),
        "GRAFANA_HOST_PORT": os.getenv("GRAFANA_HOST_PORT", "3000"),
        "AIOPS_USER_TOKEN": user_token,
    }
    ENV_FILE.write_text("\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8")
    (RUNTIME / "secrets" / "alertmanager_token").write_text(alert_token, encoding="utf-8")
    return values


def compose(*args: str) -> int:
    return subprocess.run(
        ["docker", "compose", "--env-file", str(ENV_FILE), *args], cwd=ROOT, check=False
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "down", "status", "logs", "show-token"], nargs="?", default="up")
    args = parser.parse_args()
    values = bootstrap()
    if args.command == "show-token":
        print(values.get("AIOPS_USER_TOKEN", ""))
        return 0
    if args.command == "up":
        subprocess.run([os.sys.executable, str(ROOT / "scripts" / "demo_fault.py"), "reset"], check=True)
        code = compose("up", "--build", "-d")
        if code == 0:
            grafana_port = os.getenv("GRAFANA_HOST_PORT", values.get("GRAFANA_HOST_PORT", "3000"))
            print("状态页: http://127.0.0.1:8000/status")
            print(f"Grafana: http://127.0.0.1:{grafana_port}")
            print("状态页 Token: " + values.get("AIOPS_USER_TOKEN", ""))
        return code
    if args.command == "down":
        return compose("down")
    if args.command == "status":
        return compose("ps")
    return compose("logs", "--tail", "200")


if __name__ == "__main__":
    raise SystemExit(main())
