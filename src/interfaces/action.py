"""ActionExecutor：动作执行器抽象接口。

core/rule_engine 只依赖本接口做「规则 → 动作」的调用与失败判定，
不关心动作如何实现（SSH / 其它）；具体实现注册到 src/registry.py。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ActionError(Exception):
    """动作执行失败（依赖缺失 / 凭据不可用 / 目标不可达 / 远端返回非零）。"""


class ActionExecutor(ABC):
    """动作执行器：execute() 接收参数字典，成功返回结果 dict，失败抛 ActionError。"""

    #: 动作名（与 rules.yaml 的 action 字段一致，registry 键）
    name: str = ""
    #: 一句话说明（给人看 / 未来 Agent 工具描述用）
    description: str = ""

    @abstractmethod
    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 params 执行动作，返回结果 dict；任何失败抛 ActionError。"""
