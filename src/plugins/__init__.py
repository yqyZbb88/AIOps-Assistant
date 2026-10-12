"""plugins：具体插件实现（monitors / actions / agent_actions / llm_clients / diagnostics）+ 共享内核 common。

import 本包即触发各子包导入；其中动作/诊断的底层共用 src/plugins/common（endpoint/ssh/audit/cmddef）：
- monitors/prometheus_monitor.py  注册 monitor "prometheus"
- actions/catalog + executor       写命令目录（config/actions.yaml）→ 通用执行器（不再注册固定动作类）
- agent_actions/catalog + source   Agent 写命令目录（config/agent_actions.yaml）→ 强制人工审批的写通道
- llm_clients/openai_compatible_client.py   注册 llm "openai_compatible"
- diagnostics/ssh_diagnostics.py   注册 diagnostic "ssh"（只读诊断，Agent 工具用）

惰性依赖策略：SSH 的 paramiko、LLM 的 langchain/dotenv 均在调用时才 import，
因此本包在「未装可选依赖」环境也能被安全导入（能力降级而非启动失败）。
"""
from src.plugins import (  # noqa: F401
    actions,
    agent_actions,
    diagnostics,
    llm_clients,
    monitors,
)

__all__ = ["monitors", "actions", "agent_actions", "llm_clients", "diagnostics"]
