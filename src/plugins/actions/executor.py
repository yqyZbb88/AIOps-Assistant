"""写命令通用执行器（WRITE 边界）：按 config/actions.yaml 目录项执行一条管理员声明的命令。

动作不再是「一个动作一个 Python 类」，而是目录（actions.yaml）里声明的命令 +
本通用执行器绑定执行：
- 命令模板/参数 schema 复用 src/plugins/common/cmddef（build_command 逐参校验 + shlex 转义）；
- 连接（endpoint）来自规则 params 的连接字段，经 common/endpoint 规约，凭据只在 .env 占位；
- sudo 只按目录项声明（管理员可信配置）；规则不能越权把非 sudo 项提权；
- 失败语义（写动作）：远端非零退出码 = 失败 → 抛 ActionError（与只读诊断相反）；
- 每次真实 SSH 尝试写审计（common/audit），成功记轻量 result（不含整段输出）。

本模块顶层不 import langchain / paramiko（paramiko 在 exec_command 调用点惰性导入）。
"""
from __future__ import annotations

import logging
from typing import Any

from src.interfaces.action import ActionError, ActionExecutor
from src.plugins.common import audit as audit_common
from src.plugins.common import cmddef as _cmddef
from src.plugins.common import ssh as ssh_common
from src.plugins.common.cmddef import CmdDef, ParamError
from src.plugins.common.endpoint import CONNECTION_FIELDS, endpoint_from_params

logger = logging.getLogger("aiops_assistant.actions.executor")


class CatalogCommandExecutor(ActionExecutor):
    """绑定一条 actions.yaml 写命令的通用执行器（name = 目录 key，规则按名引用）。"""

    def __init__(self, cmd: CmdDef) -> None:
        self.cmd = cmd
        self.name = cmd.key
        self.description = cmd.summary or f"执行写命令 {cmd.key}（来自 config/actions.yaml）"

    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 params 执行绑定命令；host 与连接字段必填，其余语义参数交目录校验。

        - 连接字段先剥离（host 必填），剩余键视为语义参数交给 build_command
          （多余/越权/非法 → ActionError，不进入 SSH 路径）；
        - 远端非零退出码 → ActionError（写动作失败语义）；SSH 连接/执行失败同样上抛。
        """
        clean = dict(params)
        try:
            endpoint = endpoint_from_params(clean)
        except ValueError as exc:
            raise ActionError(f"动作 {self.name} 参数不合法: {exc}") from exc

        semantic = {k: v for k, v in clean.items() if k not in CONNECTION_FIELDS}
        try:
            command = _cmddef.build_command(self.cmd, semantic)
        except ParamError as exc:
            raise ActionError(f"动作 {self.name} 参数不合法: {exc}") from exc

        host = endpoint.host
        started_at = audit_common.now_iso()
        try:
            code, out, err = ssh_common.exec_command(endpoint, command, sudo=self.cmd.sudo)
        except ssh_common.SshError as exc:
            audit_common.record_execution(
                action=self.name, command=command, host=host,
                params=clean, started_at=started_at, error=str(exc),
            )
            raise ActionError(str(exc)) from exc

        if code != 0:
            msg = f"命令执行失败 rc={code} host={host}: {err or out}"
            audit_common.record_execution(
                action=self.name, command=command, host=host,
                params=clean, started_at=started_at, error=msg,
            )
            raise ActionError(msg)

        audit_common.record_execution(
            action=self.name, command=command, host=host,
            params=clean, started_at=started_at,
            result={"ok": True, "exit_code": code},
        )
        logger.info("已执行命令 action=%s host=%s", self.name, host)
        return {
            "ok": True,
            "host": host,
            "command": command,
            "exit_code": code,
            "output": out or "(无输出)",
        }


def build_catalog_executors(cfg) -> dict[str, CatalogCommandExecutor]:
    """把一份 ActionsConfig（目录）实例化为 {目录 key: 通用执行器}。

    供 container.executors() 使用：executors map 语义与 RuleEngine 期望一致
    （key = rules.yaml 的 action 字段 → execute(params)）。
    """
    return {cmd.key: CatalogCommandExecutor(cmd) for cmd in cfg.commands}
