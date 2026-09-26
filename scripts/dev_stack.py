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


def local_admin_token() -> str:
    """返回本地 Python 启动配置中的管理员 Token，不读取 Compose 凭据。"""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise RuntimeError("缺少 python-dotenv，请先安装 requirements.txt") from exc

    values = dotenv_values(ROOT / ".env")
    raw_tokens = os.getenv("OPS_API_TOKENS") or values.get("OPS_API_TOKENS") or ""
    raw_admins = os.getenv("OPS_ADMIN_IDENTITIES") or values.get("OPS_ADMIN_IDENTITIES") or '["admin"]'
    try:
        tokens = json.loads(raw_tokens)
        admins = json.loads(raw_admins)
    except json.JSONDecodeError as exc:
        raise RuntimeError("本地 OPS_API_TOKENS 或 OPS_ADMIN_IDENTITIES 不是合法 JSON") from exc
    if not isinstance(tokens, dict) or not isinstance(admins, list):
        raise RuntimeError("本地 Token 映射或管理员身份列表格式错误")
    for token, identity in tokens.items():
        if isinstance(token, str) and token and identity in admins:
            return token
    raise RuntimeError("本地配置中没有映射到管理员身份的 Token")


def bootstrap() -> dict[str, str]:
    for name in ("assistant-data", "assistant-logs", "secrets", "textfile"):
        (RUNTIME / name).mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        RUNTIME.chmod(0o700)
        (RUNTIME / "secrets").chmod(0o700)
        for secret_file in (ENV_FILE, RUNTIME / "secrets" / "alertmanager_token"):
            if secret_file.exists():
                secret_file.chmod(0o600)
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
    if os.name == "posix":
        ENV_FILE.chmod(0o600)
        (RUNTIME / "secrets" / "alertmanager_token").chmod(0o600)
    return values


def compose(*args: str) -> int:
    # Compose 解析时外层进程环境优先于 --env-file。明确锁定开发栈生成的身份映射，
    # 否则 show-token 展示的密钥可能与真正注入容器的密钥不一致。
    values = bootstrap()
    environment = os.environ.copy()
    for name in ("OPS_API_TOKENS", "OPS_APPROVER_IDENTITIES"):
        if name in values:
            environment[name] = values[name]
    return subprocess.run(
        ["docker", "compose", "--env-file", str(ENV_FILE), *args],
        cwd=ROOT,
        env=environment,
        check=False,
    ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "down", "status", "logs", "show-token"], nargs="?", default="up")
    parser.add_argument("--source", choices=["compose", "local"], default="compose",
                        help="show-token 的凭据来源：Compose 开发栈或本地 Python 服务")
    args = parser.parse_args()
    if args.command == "show-token":
        if args.source == "local":
            try:
                print(local_admin_token())
            except RuntimeError as exc:
                parser.error(str(exc))
        else:
            print(bootstrap().get("AIOPS_USER_TOKEN", ""))
        return 0
    if args.source != "compose":
        parser.error("--source local 仅适用于 show-token")
    values = bootstrap()
    if args.command == "up":
        subprocess.run([os.sys.executable, str(ROOT / "scripts" / "demo_fault.py"), "reset"], check=True)
        code = compose("up", "--build", "-d")
        if code == 0:
            grafana_port = os.getenv("GRAFANA_HOST_PORT", values.get("GRAFANA_HOST_PORT", "3000"))
            print("控制台: http://127.0.0.1:8000/console")
            print(f"Grafana: http://127.0.0.1:{grafana_port}")
            print("Compose 控制台 Token: " + values.get("AIOPS_USER_TOKEN", ""))
        return code
    if args.command == "down":
        return compose("down")
    if args.command == "status":
        return compose("ps")
    return compose("logs", "--tail", "200")


if __name__ == "__main__":
    raise SystemExit(main())
