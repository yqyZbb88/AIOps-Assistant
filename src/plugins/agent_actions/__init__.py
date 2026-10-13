"""Agent 写命令插件族（WRITE 边界 · 仅 Agent 路径）。

与前两条边界的定位区分：
- plugins/diagnostics/ —— 只读诊断（恒不 sudo），Agent 可直接调用；
- plugins/actions/     —— rules.yaml 规则的写命令，**无头自动执行**；
- plugins/agent_actions/（本包）—— Agent 申请执行的写命令，**每次强制人工审批**。

**刻意不注册进 registry**：对齐 plugins/actions/ 的先例——该族只有一个实现，
且连接信息由容器从 hosts.yaml 的登记与凭据引用解析后注入，没有「按 provider 选实现」的需求。
将来真出现第二种实现（如 WinRM）再考虑注册。

导入本包不做任何 I/O 与重量级导入（paramiko 在 exec_command 调用点惰性加载）。
"""
from __future__ import annotations

from src.plugins.agent_actions import catalog, source  # noqa: F401  （暴露子模块，无副作用）
