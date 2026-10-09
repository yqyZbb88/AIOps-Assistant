"""跨平台 Compose 便捷入口；宿主机只需 Python 标准库和 Docker。"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    parser.add_argument("--wait-timeout", type=int, default=180, help="等待就绪的秒数，不含构建时间")
    args = parser.parse_args()
    if args.wait_timeout < 1:
        parser.error("--wait-timeout 必须大于 0")
    try:
        if args.command in {"show-token", "show-grafana-password"}:
            print(compose_secret(grafana=args.command == "show-grafana-password"))
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
                print("查看地址：python scripts/stack.py status")
                print("查看管理员 Token：python scripts/stack.py show-token")
            else:
                print("部署未就绪；运行 python scripts/stack.py status / logs 排查。数据已保留。", file=sys.stderr)
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
