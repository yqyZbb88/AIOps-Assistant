"""插件注册表：monitors / llms / diagnostics 三类插件按名注册与取用。

- 插件（plugins/ 下各包）在 import 时把实现类注册进来；
- container 依据 settings（monitor.provider / llm.provider 等）按名取类并实例化；
- 注册表本身不 import 任何插件（避免环依赖），只做「名 → 实现类」的登记；
- **写动作不走注册表**：M1b 起动作由 config/actions.yaml 写命令目录声明，
  生产实现由 container.executors() 惰性构建（见 src/plugins/actions/）。
"""
from __future__ import annotations


class Registry:
    """插件注册表（按类别分组，键为插件名）。"""

    def __init__(self) -> None:
        self._monitors: dict[str, type] = {}
        self._llms: dict[str, type] = {}
        self._diagnostics: dict[str, type] = {}

    # -- 注册 -------------------------------------------------------------
    def register_monitor(self, name: str, entry: type) -> None:
        self._monitors[name] = entry

    def register_llm(self, name: str, entry: type) -> None:
        self._llms[name] = entry

    def register_diagnostic(self, name: str, entry: type) -> None:
        self._diagnostics[name] = entry

    # -- 取用 -------------------------------------------------------------
    def monitor_class(self, name: str) -> type:
        """返回监控源实现类；未注册抛 KeyError（由 container 转清晰报错）。"""
        return self._monitors[name]

    def llm_class(self, name: str) -> type:
        return self._llms[name]

    def diagnostic_class(self, name: str) -> type:
        """返回只读诊断源实现类；未注册抛 KeyError（由 container 转清晰报错）。"""
        return self._diagnostics[name]

    # -- 查询 -------------------------------------------------------------
    def monitor_names(self) -> list[str]:
        return sorted(self._monitors)

    def llm_names(self) -> list[str]:
        return sorted(self._llms)

    def diagnostic_names(self) -> list[str]:
        """已注册的只读诊断源名（diagnostics.yaml 的 provider 字段应在此集合内）。"""
        return sorted(self._diagnostics)

    def has_monitor(self, name: str) -> bool:
        return name in self._monitors

    def has_llm(self, name: str) -> bool:
        return name in self._llms

    def has_diagnostic(self, name: str) -> bool:
        return name in self._diagnostics


# 全局单例：插件包 import 时往这里注册，container 从这里取
registry = Registry()
