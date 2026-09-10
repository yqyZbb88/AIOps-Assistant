"""插件注册表：monitors / actions / llms / diagnostics 四类插件按名注册与取用。

- 插件（plugins/ 下各包）在 import 时把实现类注册进来；
- container 依据 settings（monitor.provider / llm.provider 等）按名取类并实例化；
- 注册表本身不 import 任何插件（避免环依赖），只做「名 → 实现类」的登记。
"""
from __future__ import annotations

from typing import Any

_MISSING = object()


class Registry:
    """插件注册表（按类别分组，键为插件名）。"""

    def __init__(self) -> None:
        self._monitors: dict[str, type] = {}
        self._actions: dict[str, type] = {}
        self._llms: dict[str, type] = {}
        self._diagnostics: dict[str, type] = {}

    # -- 注册 -------------------------------------------------------------
    def register_monitor(self, name: str, entry: type) -> None:
        self._monitors[name] = entry

    def register_action(self, name: str, entry: type) -> None:
        self._actions[name] = entry

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

    def action_class(self, name: str, default: Any = _MISSING) -> type:
        """返回动作实现类；未注册且给了 default 时返回 default。"""
        if default is not _MISSING:
            return self._actions.get(name, default)
        return self._actions[name]

    def diagnostic_class(self, name: str, default: Any = _MISSING) -> type:
        """返回只读诊断源实现类；未注册且给了 default 时返回 default。"""
        if default is not _MISSING:
            return self._diagnostics.get(name, default)
        return self._diagnostics[name]

    # -- 查询 -------------------------------------------------------------
    def monitor_names(self) -> list[str]:
        return sorted(self._monitors)

    def llm_names(self) -> list[str]:
        return sorted(self._llms)

    def action_names(self) -> list[str]:
        """已注册的动作名（rules.yaml 的 action 字段应在此集合内）。"""
        return sorted(self._actions)

    def diagnostic_names(self) -> list[str]:
        """已注册的只读诊断源名（diagnostics.yaml 的 provider 字段应在此集合内）。"""
        return sorted(self._diagnostics)

    def all_action_classes(self) -> dict[str, type]:
        """返回 {动作名: 实现类} 副本，供 container 实例化全部执行器。"""
        return dict(self._actions)

    def has_action(self, name: str) -> bool:
        return name in self._actions

    def has_monitor(self, name: str) -> bool:
        return name in self._monitors

    def has_llm(self, name: str) -> bool:
        return name in self._llms

    def has_diagnostic(self, name: str) -> bool:
        return name in self._diagnostics


# 全局单例：插件包 import 时往这里注册，container 从这里取
registry = Registry()
