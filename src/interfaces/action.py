"""ActionExecutor：动作执行器抽象接口。

core/rule_engine 只依赖本接口做「规则 → 动作」的调用与失败判定，
不关心动作如何实现（SSH / 其它）；具体实现由 container.executors() 按 config/actions.yaml
目录构建并注入（**actions 不走 registry**）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol


class ActionError(Exception):
    """动作执行失败（依赖缺失 / 凭据不可用 / 目标不可达 / 远端返回非零）。"""


class ActionExecutor(ABC):
    """动作执行器：execute() 接收参数字典，成功返回结果 dict，失败抛 ActionError。"""

    #: 动作名（与 rules.yaml 的 action 字段一致，即 actions.yaml 目录 key）
    name: str = ""
    #: 一句话说明（给人看 / 未来 Agent 工具描述用）
    description: str = ""

    @abstractmethod
    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 params 执行动作，返回结果 dict；任何失败抛 ActionError。"""


class RuleExecutionGuard(Protocol):
    """自动处置执行前的原子抢占口。

    ``claim`` 返回 True 表示本次调用获得执行权；返回 False 表示同一告警事件
    的同一动作已经被处理过，调用方必须静默跳过写操作。实现必须保证并发原子性，
    并在存储故障时 fail-closed（返回 False 或抛异常，绝不能放行）。
    """

    def claim(
        self,
        *,
        alert_key: str,
        action: str,
        alert_status: str,
        starts_at: str,
    ) -> bool:
        """抢占一次规则执行权。"""
        ...
