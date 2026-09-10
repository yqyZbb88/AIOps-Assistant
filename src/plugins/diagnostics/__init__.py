"""只读诊断源插件：导入即注册到 registry（键 = 诊断源 provider 名）。

当前注册：
- ssh（ssh_diagnostics.py）— 经 SSH 执行 config/diagnostics.yaml 白名单只读命令

诊断源实现 DiagnosticSource 接口（interfaces/diagnostic.py），由 container.diagnostics()
按 diagnostics.yaml 的 provider 取名实例化。顶层不 import paramiko（惰性，见 ssh_diagnostics）。
"""
from src.plugins.diagnostics import commands, ssh_diagnostics  # noqa: F401
from src.registry import registry

registry.register_diagnostic(
    ssh_diagnostics.SshDiagnostics.name, ssh_diagnostics.SshDiagnostics
)

__all__ = ["commands", "ssh_diagnostics"]
