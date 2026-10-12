"""共享 SSH 执行内核：主机密钥拒绝优先、惰性导入、流式有界读取。"""
from __future__ import annotations

import shlex
import time
from dataclasses import dataclass
from typing import Iterator

from src.plugins.common.endpoint import SshEndpoint
from src.utils.security import redact_text, register_secrets


class SshError(Exception):
    """SSH 连接、认证或执行失败。"""


class SshOutcomeUnknown(SshError):
    """命令可能已到达远端，不能因连接异常自动重试。"""


@dataclass(frozen=True)
class SshCommandResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False
    bytes_read: int = 0

    def __iter__(self) -> Iterator[object]:
        """兼容既有 ``code, out, err = result`` 调用。"""
        yield self.exit_code
        yield self.stdout
        yield self.stderr


def _new_client(paramiko, endpoint: SshEndpoint):
    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
        if endpoint.known_hosts:
            client.load_host_keys(endpoint.known_hosts)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    except Exception:
        # 初始化失败时调用方拿不到 client，必须在此释放并保留原始错误。
        try:
            client.close()
        except Exception:
            pass
        raise
    return client


def _read_channel(channel, *, output_limit: int, timeout: float) -> SshCommandResult:
    """排空 stdout/stderr，但只保留共享字节预算内的数据。"""
    budget = max(int(output_limit), 1) * 4
    out = bytearray()
    err = bytearray()
    total = 0
    truncated = False
    deadline = time.monotonic() + max(float(timeout), 0.001)

    while True:
        progressed = False
        for ready_name, recv_name, target in (
            ("recv_ready", "recv", out),
            ("recv_stderr_ready", "recv_stderr", err),
        ):
            if getattr(channel, ready_name)():
                chunk = getattr(channel, recv_name)(32768)
                if chunk:
                    progressed = True
                    total += len(chunk)
                    remaining = max(budget - len(out) - len(err), 0)
                    if remaining:
                        target.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            break
        if time.monotonic() >= deadline:
            channel.close()
            raise SshOutcomeUnknown(f"SSH 命令执行超时（{timeout:g} 秒）")
        if not progressed:
            time.sleep(0.01)

    code = int(channel.recv_exit_status())
    stdout = redact_text(out.decode(errors="replace").strip())
    stderr = redact_text(err.decode(errors="replace").strip())
    if len(stdout) > output_limit:
        stdout, truncated = stdout[:output_limit], True
    if len(stderr) > output_limit:
        stderr, truncated = stderr[:output_limit], True
    return SshCommandResult(code, stdout, stderr, truncated, total)


def exec_command(
    endpoint: SshEndpoint,
    command: str,
    *,
    sudo: bool = False,
    output_limit: int = 4000,
) -> SshCommandResult:
    """执行调用方已授权并构建的命令；本函数不校验业务白名单。

    使用主机密钥拒绝策略，按阶段超时并有界保留两流输出。尝试派发后传输失败
    抛 SshOutcomeUnknown，不能据此认定未执行；其它 SSH 失败抛 SshError。
    """
    try:
        import paramiko
    except ImportError as exc:
        raise SshError("缺少可选依赖 paramiko，请先安装：pip install paramiko") from exc

    register_secrets(endpoint.password)
    full_cmd = f"sudo sh -c {shlex.quote(command)}" if sudo else command
    try:
        client = _new_client(paramiko, endpoint)
    except Exception as exc:
        raise SshError(f"加载 SSH 主机密钥配置失败: {redact_text(exc)}") from exc
    dispatch_attempted = False
    try:
        client.connect(
            hostname=endpoint.host,
            port=int(endpoint.port),
            username=endpoint.username,
            key_filename=endpoint.key_filename,
            password=endpoint.password or None,
            timeout=endpoint.connect_timeout,
            banner_timeout=endpoint.connect_timeout,
            auth_timeout=endpoint.connect_timeout,
            channel_timeout=endpoint.connect_timeout,
            allow_agent=endpoint.key_filename is None,
            look_for_keys=endpoint.key_filename is None,
        )
        dispatch_attempted = True
        _, stdout, _ = client.exec_command(full_cmd, timeout=endpoint.connect_timeout)
        return _read_channel(
            stdout.channel,
            output_limit=max(int(output_limit), 1),
            timeout=endpoint.connect_timeout,
        )
    except SshError:
        raise
    except Exception as exc:
        error_type = SshOutcomeUnknown if dispatch_attempted else SshError
        raise error_type(
            f"SSH 执行失败 host={endpoint.host}: {redact_text(exc)}"
        ) from exc
    finally:
        client.close()
