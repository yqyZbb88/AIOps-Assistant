"""ActionExecutor：动作执行器抽象接口。

core/rule_engine 只依赖本接口做「规则 → 动作」的调用与失败判定，
不关心动作如何实现（SSH / 其它）；具体实现由 container.executors() 按 config/actions.yaml
目录构建并注入（**actions 不走 registry**）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Protocol
from src.interfaces.host import HostSshTarget


class ActionError(Exception):
    """动作执行失败（依赖缺失 / 凭据不可用 / 目标不可达 / 远端返回非零）。"""


class ActionOutcomeUnknown(ActionError):
    """派发后传输异常或缺少退出状态，远端可能已写入，调用方不得自动重试。"""


class ActionExecutor(ABC):
    """动作执行器：execute() 接收参数字典，成功返回结果 dict，失败抛 ActionError。"""

    #: 动作名（与 rules.yaml 的 action 字段一致，即 actions.yaml 目录 key）
    name: str = ""
    #: 动作说明，供日志和界面展示
    description: str = ""

    @abstractmethod
    def execute(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 params 执行动作，返回结果 dict；任何失败抛 ActionError。"""

    def execute_on(
        self, target: HostSshTarget, params: dict[str, Any]
    ) -> dict[str, Any]:
        """在显式主机目标执行；多主机路径必须调用本方法。"""
        raise ActionError(f"动作 {self.name} 不支持显式主机目标")


class RuleExecutionGuard(Protocol):
    """规则执行前原子抢占；True 获权，False 表示保留期内重复。

    实现须保证并发原子性；存储故障返回 False 或抛异常，不能放行。保护范围受
    保留期、持久化与备份恢复影响，不承诺无限期 exactly-once。
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
