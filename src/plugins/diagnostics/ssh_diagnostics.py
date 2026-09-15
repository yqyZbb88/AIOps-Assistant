"""只读诊断源插件（SshDiagnostics）：经 SSH 执行 config/diagnostics.yaml 白名单只读命令。

- 连接/认证/执行复用 src/plugins/common/ssh.py 的 exec_command（paramiko 惰性导入），
  审计走 common/audit.py（与动作执行同通道统一结构）；
- 目标主机与凭据来自构造时注入的 DiagnosticsConfig（config/diagnostics.yaml 加载，
  password 为 .env 占位插值），**模型上下文与工具参数里永无凭据**；
- run() 只接受「命令名 + 语义参数」，参数经 commands.py 全匹配/取值/转义后拼进模板；
- sudo 恒 False（模板不含 sudo 也不给开 sudo 的口）；每条真实 SSH 尝试都写审计。

边界语义（与动作「写」相反，属「读」）：
- 远端非零退出码**不算失败**：grep 无匹配、systemctl 查 failed 服务常返回非 0，
  输出照常回传（ok=False），由 Agent 自行判断；
- 连接 / 认证 / 未知命令 / 参数非法 → 抛 DiagnosticError（工具层捕获转可读文本）。
"""
from __future__ import annotations

import logging
from typing import Any

from src.interfaces.diagnostic import CommandSpec, DiagnosticError, DiagnosticSource
from src.plugins.common import audit as audit_common
from src.plugins.common import ssh as ssh_common
from src.plugins.common.endpoint import SshEndpoint
from src.plugins.diagnostics.commands import (
    DiagnosticsConfig,
    build_command,
    command_specs,
)

logger = logging.getLogger("ops_agent.diagnostics.ssh")


class SshDiagnostics(DiagnosticSource):
    """按 config/diagnostics.yaml 白名单经 SSH 执行只读命令的诊断源。"""

    name = "ssh"
    description = "经 SSH 在目标主机执行白名单只读诊断命令（free/df/ps/systemctl/journalctl 等）"

    def __init__(self, config: DiagnosticsConfig) -> None:
        # 构造不读文件：config 由容器/测试注入，便于离线单测
        if config is None or not config.target.host:
            raise DiagnosticError("诊断配置缺少 target.host，无法构造 SshDiagnostics")
        self.config = config
        self.target = config.target
        self.output_limit = config.output_limit

    # -- DiagnosticSource --------------------------------------------------
    def commands(self) -> tuple[CommandSpec, ...]:
        return command_specs(self.config)

    def run(
        self,
        command: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        diag_cmd = next((c for c in self.config.commands if c.key == command), None)
        if diag_cmd is None:
            available = ", ".join(c.key for c in self.config.commands) or "(空)"
            raise DiagnosticError(f"未知诊断命令 {command!r}，可用: {available}")

        # 参数校验/默认/越权/全匹配 + 逐参 shlex 转义 → 安全命令串（非法抛 DiagnosticError）
        cmd_str = build_command(diag_cmd, params)

        host = self.target.host
        endpoint = SshEndpoint(
            host=self.target.host,
            port=int(self.target.port),
            username=self.target.username or "root",
            key_filename=self.target.key_filename or None,
            known_hosts=self.target.known_hosts or None,
            password=self.target.password or None,
            connect_timeout=int(self.target.connect_timeout),
        )
        started_at = audit_common.now_iso()
        try:
            code, out, err = ssh_common.exec_command(endpoint, cmd_str, sudo=False)
        except ssh_common.SshError as exc:  # paramiko 缺失 / 连接 / 认证 / 执行失败
            audit_common.record_execution(
                action="diagnostics", command=command, host=host,
                params=dict(params or {}), started_at=started_at, error=str(exc),
            )
            raise DiagnosticError(f"SSH 诊断执行失败（host={host}）: {exc}") from exc

        raw_output = (out or err).strip() or "(无输出)"
        truncated = len(raw_output) > self.output_limit
        output = raw_output[: self.output_limit]
        if truncated:
            output += f"\n…（输出超过 {self.output_limit} 字符已截断）"
        audit_common.record_execution(
            action="diagnostics", command=command, host=host,
            params=dict(params or {}), started_at=started_at,
            result={"exit_code": code, "ok": code == 0, "truncated": truncated,
                    "chars": len(raw_output)},
        )
        return {
            "ok": code == 0,
            "host": host,
            "command": command,
            "exit_code": code,
            "output": output,
        }
