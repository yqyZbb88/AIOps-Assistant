"""Agent 写命令源插件（SshAgentActionSource）：经 SSH 执行 config/agent_actions.yaml 白名单写命令。

与只读诊断（plugins/diagnostics/ssh_diagnostics.py）是**近亲但边界相反**：
- 读：恒不 sudo；远端非零退出码**不算失败**（grep 无匹配 / systemctl 查 failed 返回 3），
      输出照常回传，ok=False 交给 Agent 判断；
- 写（本模块）：sudo 按目录逐条声明；远端非零退出码**就是失败** → 抛 AgentActionError。

安全要点（与只读诊断一致的部分）：
- 连接凭据只在构造时注入的 AgentActionsConfig.target 里（密码为 .env 占位插值），
  **模型上下文与工具参数里永无 host/密码/私钥**；
- run() 只接受「命令名 + 语义参数」；params 里若出现任何连接字段（host/port/…）一律拒绝
  （工具层本就不会生成这些字段，这里是纵深防御）；
- 参数经 common/cmddef 全匹配 + shlex 转义后拼进模板；
- 每次真实 SSH 尝试写审计（common/audit，params 打码，不落密码、不落整段输出）。

**审批不在此层**：本模块的 run() 一旦被调用就真的会执行。审批由 Agent 编排层
（HumanInTheLoopMiddleware）强制前置，绕不过去；无头规则路径（RuleEngine）不使用本模块。
"""
from __future__ import annotations

import logging
from typing import Any

from src.interfaces.agent_action import AgentActionError, AgentActionSource
from src.interfaces.command import CommandSpec
from src.plugins.common import audit as audit_common
from src.plugins.common import ssh as ssh_common
from src.plugins.common.endpoint import CONNECTION_FIELDS, SshEndpoint
from src.plugins.agent_actions.catalog import (
    AgentActionsConfig,
    build_command,
    command_specs,
)

logger = logging.getLogger("aiops_assistant.agent_actions.ssh")


class SshAgentActionSource(AgentActionSource):
    """按 config/agent_actions.yaml 白名单经 SSH 执行写命令的写命令源。"""

    name = "ssh"
    description = "经 SSH 在目标主机执行白名单写命令（重启服务 / 清理磁盘等，需人工审批）"

    def __init__(self, config: AgentActionsConfig) -> None:
        # 构造不读文件：config 由容器/测试注入，便于离线单测
        if config is None or not config.target.host:
            raise AgentActionError("Agent 写命令配置缺少 target.host，无法构造 SshAgentActionSource")
        self.config = config
        self.target = config.target
        self.output_limit = config.output_limit

    # -- AgentActionSource 接口实现 -----------------------------------------
    def commands(self) -> tuple[CommandSpec, ...]:
        return command_specs(self.config)

    def run(
        self,
        command: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cmd_def = next((c for c in self.config.commands if c.key == command), None)
        if cmd_def is None:
            available = ", ".join(c.key for c in self.config.commands) or "(空)"
            raise AgentActionError(f"未知写命令 {command!r}，可用: {available}")

        # 纵深防御：语义参数里绝不允许夹带连接字段（工具层不会生成，防的是别处误传）
        supplied = dict(params or {})
        leaked = [k for k in supplied if k in CONNECTION_FIELDS]
        if leaked:
            raise AgentActionError(
                f"写命令 {command} 不接受连接参数: {', '.join(sorted(leaked))}"
                "（连接凭据由服务端配置提供，不得由调用方指定）"
            )

        # 参数校验/默认/越权/全匹配 + 逐参 shlex 转义 → 安全命令串（非法抛 AgentActionError）
        cmd_str = build_command(cmd_def, supplied)

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
            result = ssh_common.exec_command(
                endpoint, cmd_str, sudo=cmd_def.sudo, output_limit=self.output_limit
            )
            code, out, err = result
        except ssh_common.SshError as exc:  # paramiko 缺失 / 连接 / 认证 / 执行失败
            audit_common.record_execution(
                action="agent_action", command=command, host=host,
                params=supplied, started_at=started_at, error=str(exc),
            )
            raise AgentActionError(f"写命令 SSH 执行失败（host={host}）: {exc}") from exc

        # 写语义：非零退出码就是失败（与只读诊断相反）
        if code != 0:
            detail = (err or out or "").strip()
            truncated = bool(getattr(result, "truncated", False))
            audit_common.record_execution(
                action="agent_action", command=command, host=host,
                params=supplied, started_at=started_at,
                error=(f"rc={code}; truncated={truncated}; chars={len(detail)}; "
                       f"bytes_read={int(getattr(result, 'bytes_read', len(detail)))}"),
            )
            raise AgentActionError(
                f"写命令执行失败（host={host}，exit_code={code}）：{detail or '(无输出)'}"
            )

        raw_output = (out or err).strip() or "(无输出)"
        truncated = bool(getattr(result, "truncated", False)) or len(raw_output) > self.output_limit
        output = raw_output[: self.output_limit]
        if truncated:
            output += f"\n…（输出超过 {self.output_limit} 字符已截断）"
        # 审计只记轻量结果，不落整段输出（与只读诊断一致）
        audit_common.record_execution(
            action="agent_action", command=command, host=host,
            params=supplied, started_at=started_at,
            result={"exit_code": code, "ok": True, "truncated": truncated,
                    "chars": len(raw_output),
                    "bytes_read": int(getattr(result, "bytes_read", len(raw_output)))},
        )
        logger.info("已执行 Agent 写命令 action=%s host=%s", command, host)
        return {
            "ok": True,
            "host": host,
            "command": command,
            "exit_code": code,
            "output": output,
            "executed_at": audit_common.now_iso(),
        }
