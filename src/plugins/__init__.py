"""plugins：具体插件实现（monitors / actions / llm_clients / diagnostics）+ 共享内核 common。

import 本包即触发各子包导入；其中动作/诊断的底层共用 src/plugins/common（endpoint/ssh/audit/cmddef）：
- monitors/prometheus_monitor.py  注册 monitor "prometheus"
- actions/catalog + executor       写命令目录（config/actions.yaml）→ 通用执行器（不再注册固定动作类）
- llm_clients/deepseek_client.py   注册 llm "deepseek"
- diagnostics/ssh_diagnostics.py   注册 diagnostic "ssh"（只读诊断，Agent 工具用）

惰性依赖策略：SSH 的 paramiko、LLM 的 langchain/dotenv 均在调用时才 import，
因此本包在「未装可选依赖」环境也能被安全导入（能力降级而非启动失败）。
"""
from src.plugins import actions, diagnostics, llm_clients, monitors  # noqa: F401

__all__ = ["monitors", "actions", "llm_clients", "diagnostics"]
