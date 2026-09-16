"""共享 SSH 执行内核：给定 SshEndpoint + 一条命令串执行，返回 (exit_code, stdout, stderr)。

- 中性：连接/认证/执行失败统一抛本模块的 SshError，由各边界自行映射
  （actions → ActionError，diagnostics → DiagnosticError），共享层不依赖任何业务异常；
- paramiko 惰性导入：未安装时抛清晰错误，不影响模块加载 / 未装依赖环境的降级；
- 认证优先级 key_filename（私钥）→ password；显式提供 key_filename 或 password 时
  不再尝试 agent/默认密钥；
- sudo=True 用 `sudo sh -c <command>` 整体包裹，保证整条命令都有 root 权限
  （是否允许 sudo 由调用边界决定，本函数只按标志执行）。
"""
from __future__ import annotations

import logging
import shlex

from src.plugins.common.endpoint import SshEndpoint


logger = logging.getLogger("aiops_assistant.ssh")


class SshError(Exception):
    """SSH 连接 / 认证 / 执行失败（共享层中性异常）。"""


def _auth_options(endpoint: SshEndpoint) -> dict[str, bool]:
    """按显式凭据决定是否允许 agent/默认密钥，避免它们抢占认证次数。"""
    explicit_credential = bool(endpoint.key_filename or endpoint.password)
    return {
        "allow_agent": not explicit_credential,
        "look_for_keys": not explicit_credential,
    }


def _new_client(paramiko, endpoint: SshEndpoint):
    """构造默认拒绝未知主机密钥的 SSH 客户端。

    先加载系统 known_hosts，再加载 endpoint 指定的 known_hosts。没有可信主机密钥时
    connect 会直接失败，而不是像 AutoAddPolicy 那样把连接交给任意返回密钥的远端。
    """
    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
    except Exception as exc:  # 系统文件不可读/损坏不应阻断显式配置的文件
        logger.warning(
            "加载系统 known_hosts 失败，已跳过: %s: %s",
            type(exc).__name__,
            exc,
        )
    if endpoint.known_hosts:
        try:
            client.load_host_keys(endpoint.known_hosts)
        except Exception as exc:
            raise SshError(
                f"加载 known_hosts 失败 ({endpoint.known_hosts}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    return client


def exec_command(
    endpoint: SshEndpoint,
    command: str,
    *,
    sudo: bool = False,
) -> tuple[int, str, str]:
    """经 SSH 在 endpoint 上执行一条命令，返回 (exit_code, stdout, stderr)。"""
    try:
        import paramiko
    except ImportError as exc:
        raise SshError("缺少可选依赖 paramiko，请先安装：pip install paramiko") from exc

    full_cmd = f"sudo sh -c {shlex.quote(command)}" if sudo else command
    try:
        client = _new_client(paramiko, endpoint)
    except SshError:
        raise
    except Exception as exc:
        raise SshError(
            f"初始化 SSH 客户端失败: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        client.connect(
            hostname=endpoint.host,
            port=int(endpoint.port),
            username=endpoint.username,
            key_filename=endpoint.key_filename,
            password=endpoint.password or None,
            timeout=endpoint.connect_timeout,
            **_auth_options(endpoint),
        )
    except Exception as exc:  # paramiko 各类连接/认证异常统一收敛
        client.close()
        raise SshError(
            f"SSH 连接 {endpoint.username}@{endpoint.host}:{endpoint.port} 失败: {exc}"
        ) from exc

    try:
        _, stdout, stderr = client.exec_command(full_cmd, timeout=endpoint.connect_timeout)
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        code = stdout.channel.recv_exit_status()
    except Exception as exc:
        raise SshError(f"SSH 执行失败 host={endpoint.host}: {exc}") from exc
    finally:
        client.close()
    return code, out, err
