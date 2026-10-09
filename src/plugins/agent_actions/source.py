"""Agent 写命令源插件（SshAgentActionSource）：经 SSH 执行 config/agent_actions.yaml 白名单写命令。

与只读诊断（plugins/diagnostics/ssh_diagnostics.py）是**近亲但边界相反**：
- 读：恒不 sudo；远端非零退出码**不算失败**（grep 无匹配 / systemctl 查 failed 返回 3），
      输出照常回传，ok=False 交给 Agent 判断；
- 写（本模块）：sudo 按目录逐条声明；远端非零退出码**就是失败** → 抛 AgentActionError。

安全要点（与只读诊断一致的部分）：
- 连接凭据只在构造时注入的 AgentActionsConfig.target 里（密码由容器环境插值或主机凭据注入），
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
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from src.interfaces.agent_action import AgentActionError, AgentActionSource
from src.interfaces.host import HostSshTarget
from src.interfaces.execution import ActionExecutionStore
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

    def __init__(
        self,
        config: AgentActionsConfig,
        *,
        target: HostSshTarget | None = None,
        allowed_commands: frozenset[str] | None = None,
    ) -> None:
        # 构造不读文件：config 由容器/测试注入，便于离线单测
        if config is None or (target is None and not config.target.host):
            raise AgentActionError("Agent 写命令配置缺少 target.host，无法构造 SshAgentActionSource")
        self.config = config
        self.target = target or config.target
        self.allowed_commands = allowed_commands
        self.output_limit = config.output_limit
        self.execution_store: ActionExecutionStore | None = None
        self.execution_host_id = ""
        self.target_summary = ""
        self.config_fingerprint = ""
        self._approval_context: ContextVar[dict[str, Any] | None] = ContextVar(
            f"agent_action_approval_{id(self)}", default=None
        )

    def configure_execution(
        self,
        store: ActionExecutionStore | None,
        *,
        host_id: str,
        target_summary: str,
        config_fingerprint: str,
    ) -> None:
        self.execution_store = store
        self.execution_host_id = host_id
        self.target_summary = target_summary
        self.config_fingerprint = config_fingerprint

    @contextmanager
    def approval_context(self, approval_id: str):
        token = self._approval_context.set({"approval_id": approval_id, "index": 0})
        try:
            yield
        finally:
            self._approval_context.reset(token)

    def record_rejection(
        self, approval_id: str, actions: list[dict[str, Any]]
    ) -> None:
        if self.execution_store is None:
            return
        for index, action in enumerate(actions):
            self.execution_store.create(
                channel="agent",
                idempotency_key=f"agent:{approval_id}:{index}",
                host_id=self.execution_host_id,
                action_name=str(action.get("name") or ""),
                params=dict(action.get("args") or {}),
                target_summary=self.target_summary,
                config_fingerprint=self.config_fingerprint,
                approval_id=approval_id,
                status="rejected",
                detail="人工审批拒绝",
            )

    def _finish_execution(self, execution_id: str, status: str, detail: str) -> None:
        try:
            self.execution_store.finish(execution_id, status=status, detail=detail)
        except Exception as exc:
            logger.error(
                "Agent 执行状态收尾失败 execution_id=%s（保留 running，结果未知）: %s",
                execution_id,
                exc,
            )

    # -- AgentActionSource 接口实现 -----------------------------------------
    def commands(self) -> tuple[CommandSpec, ...]:
        specs = command_specs(self.config)
        if self.allowed_commands is None:
            return specs
        return tuple(spec for spec in specs if spec.key in self.allowed_commands)

    def run(
        self,
        command: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cmd_def = next((c for c in self.config.commands if c.key == command), None)
        if self.allowed_commands is not None and command not in self.allowed_commands:
            raise AgentActionError(f"目标主机未授权写命令 {command!r}")
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

        execution = None
        context = self._approval_context.get()
        if self.execution_store is not None:
            if context is None:
                raise AgentActionError("写命令缺少审批执行上下文，已拒绝执行")
            index = int(context["index"])
            context["index"] = index + 1
            try:
                execution = self.execution_store.create(
                    channel="agent",
                    idempotency_key=f"agent:{context['approval_id']}:{index}",
                    host_id=self.execution_host_id,
                    action_name=command,
                    params=supplied,
                    target_summary=self.target_summary,
                    config_fingerprint=self.config_fingerprint,
                    approval_id=str(context["approval_id"]),
                    status="running",
                )
            except Exception as exc:
                raise AgentActionError(f"无法创建执行审计记录，已拒绝写命令: {exc}") from exc
            if execution is None:
                raise AgentActionError("相同审批动作已执行或正在执行，拒绝重复写命令")

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
        except ssh_common.SshOutcomeUnknown as exc:
            if execution is not None:
                self._finish_execution(execution.execution_id, "unknown", str(exc))
            audit_common.record_execution(
                action="agent_action", command=command, host=host,
                params=supplied, started_at=started_at, error=str(exc),
            )
            raise AgentActionError("Remote write result is unknown; verify target state before any new attempt") from exc
        except ssh_common.SshError as exc:  # paramiko 缺失 / 连接 / 认证 / 执行失败
            if execution is not None:
                self._finish_execution(execution.execution_id, "failed", str(exc))
            audit_common.record_execution(
                action="agent_action", command=command, host=host,
                params=supplied, started_at=started_at, error=str(exc),
            )
            public_target = self.target_summary or self.execution_host_id or "服务端绑定目标"
            safe_error = str(exc).replace(host, public_target)
            raise AgentActionError(
                f"写命令 SSH 执行失败（target={public_target}）: {safe_error}"
            ) from exc

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
            if execution is not None:
                self._finish_execution(execution.execution_id, "failed", detail)
            public_target = self.target_summary or self.execution_host_id or "服务端绑定目标"
            raise AgentActionError(
                f"写命令执行失败（target={public_target}，exit_code={code}）："
                f"{detail or '(无输出)'}"
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
        if execution is not None:
            self._finish_execution(
                execution.execution_id, "succeeded", "写命令执行成功"
            )
        return {
            "ok": True,
            "host": host,
            "host_id": self.execution_host_id,
            "target_summary": self.target_summary,
            "command": command,
            "exit_code": code,
            "output": output,
            "executed_at": audit_common.now_iso(),
            "execution_id": execution.execution_id if execution is not None else None,
        }
