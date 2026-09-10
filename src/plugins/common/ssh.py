"""共享 SSH 执行内核：给定 SshEndpoint + 一条命令串执行，返回 (exit_code, stdout, stderr)。

- 中性：连接/认证/执行失败统一抛本模块的 SshError，由各边界自行映射
  （actions → ActionError，diagnostics → DiagnosticError），共享层不依赖任何业务异常；
- paramiko 惰性导入：未安装时抛清晰错误，不影响模块加载 / 未装依赖环境的降级；
- 认证优先级 key_filename（私钥）→ password；显式给了 key_filename 时不再试 agent/默认密钥；
- sudo=True 用 `sudo sh -c <command>` 整体包裹，保证整条命令都有 root 权限
  （是否允许 sudo 由调用边界决定，本函数只按标志执行）。
"""
from __future__ import annotations

import shlex

from src.plugins.common.endpoint import SshEndpoint


class SshError(Exception):
    """SSH 连接 / 认证 / 执行失败（共享层中性异常）。"""


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

    client = paramiko.SSHClient()
    # 演示环境放宽 host key 校验；生产建议先核对 known_hosts 再改 RejectPolicy
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=endpoint.host,
            port=int(endpoint.port),
            username=endpoint.username,
            key_filename=endpoint.key_filename,
            password=endpoint.password or None,
            timeout=endpoint.connect_timeout,
            allow_agent=endpoint.key_filename is None,
            look_for_keys=endpoint.key_filename is None,
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
