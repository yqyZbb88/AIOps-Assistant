"""跨平台 Compose 便捷入口；宿主机无需项目依赖（local Token 查询除外）。"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def compose_command(*args: str) -> list[str]:
    return ["docker", "compose", "--project-directory", str(ROOT), "-f", str(ROOT / "compose.yaml"), *args]


def doctor() -> None:
    if shutil.which("docker") is None:
        raise RuntimeError("未找到 Docker，请安装并启动 Docker Desktop 或 Docker Engine + Compose 插件")
    checks = [
        (["docker", "compose", "version", "--short"], "Docker Compose 不可用，需要 2.24 或以上版本"),
        (["docker", "info", "--format", "{{.ServerVersion}}"], "Docker 引擎未就绪，请启动 Docker Desktop / Docker 服务"),
        (compose_command("config", "--quiet"), "Compose 配置无效，请检查 .env 中的端口和路径"),
    ]
    for index, (command, message) in enumerate(checks):
        try:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=25, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise RuntimeError(message) from None
        if result.returncode:
            raise RuntimeError(message)
        if index == 0:
            match = re.search(r"(\d+)\.(\d+)", result.stdout)
            if not match or tuple(map(int, match.groups())) < (2, 24):
                raise RuntimeError(message)


def compose(*args: str) -> int:
    return subprocess.run(compose_command(*args), cwd=ROOT, check=False).returncode


def compose_secret(*, grafana: bool = False) -> str:
    command = "show-grafana-password" if grafana else "show-token"
    result = subprocess.run(compose_command("run", "--rm", "--no-deps", "init", command),
                            cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("无法读取 Compose 凭据，请检查初始化状态和 Docker 引擎")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "down", "status", "logs", "doctor", "show-token", "show-grafana-password"], nargs="?", default="up")
    parser.add_argument("--source", choices=["compose", "local"], default="compose",
                        help="show-token 的凭据来源")
    parser.add_argument("--wait-timeout", type=int, default=180, help="等待就绪的秒数，不含构建时间")
    args = parser.parse_args()
    if args.source != "compose" and args.command != "show-token":
        parser.error("--source local 仅适用于 show-token")
    if args.wait_timeout < 1:
        parser.error("--wait-timeout 必须大于 0")
    try:
        if args.command in {"show-token", "show-grafana-password"}:
            print(local_admin_token() if args.source == "local" else compose_secret(grafana=args.command == "show-grafana-password"))
            return 0
        if args.command in {"up", "doctor"}:
            doctor()
        if args.command == "doctor":
            print("Docker、Compose 与部署配置检查通过。")
            return 0
        if args.command == "up":
            code = compose("up", "--build", "--wait", "--wait-timeout", str(args.wait_timeout))
            if code == 0:
                print("服务已就绪。默认控制台 http://127.0.0.1:8000/console；自定义端口见 .env。")
                print("查看地址：python scripts/dev_stack.py status")
                print("查看管理员 Token：python scripts/dev_stack.py show-token")
            else:
                print("部署未就绪；运行 python scripts/dev_stack.py status / logs 排查。数据已保留。", file=sys.stderr)
            return code
        if args.command == "down":
            return compose("down")
        if args.command == "status":
            return compose("ps", "--all")
        return compose("logs", "--tail", "200")
    except (OSError, RuntimeError, ValueError, KeyError, StopIteration) as exc:
        if args.command in {"show-token", "show-grafana-password"}:
            print("无法读取凭据，请检查部署状态、目录权限和凭据来源。Docker 方式见 docs/deployment.md。", file=sys.stderr)
        else:
            print(f"部署失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
