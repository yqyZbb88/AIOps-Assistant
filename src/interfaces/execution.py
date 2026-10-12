"""规则自动处置与 Agent 审批写命令共用的执行状态模型。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ActionExecution:
    """一次规则或 Agent 动作的持久执行快照；unknown/running 需结合远端对账。"""
    execution_id: str
    channel: str
    idempotency_key: str
    host_id: str
    action_name: str
    params: dict[str, Any]
    target_summary: str = ""
    config_fingerprint: str = ""
    approval_id: str = ""
    alert_key: str = ""
    status: str = "pending"
    detail: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "channel": self.channel,
            "host_id": self.host_id,
            "action_name": self.action_name,
            "params": self.params,
            "target_summary": self.target_summary,
            "config_fingerprint": self.config_fingerprint,
            "approval_id": self.approval_id or None,
            "alert_key": self.alert_key or None,
            "status": self.status,
            "detail": self.detail,
            "created_at": self.created_at,
            "started_at": self.started_at or None,
            "finished_at": self.finished_at or None,
        }


class ActionExecutionStore(ABC):
    """执行审计与幂等契约；创建记录和结束状态不等同远端事务提交。"""
    @abstractmethod
    def create(
        self,
        *,
        channel: str,
        idempotency_key: str,
        host_id: str,
        action_name: str,
        params: dict[str, Any],
        target_summary: str = "",
        config_fingerprint: str = "",
        approval_id: str = "",
        alert_key: str = "",
        status: str = "pending",
        detail: str = "",
    ) -> ActionExecution | None:
        """原子创建；幂等键已存在时返回 None。"""

    @abstractmethod
    def finish(self, execution_id: str, *, status: str, detail: str = "") -> None:
        """结束一次执行并写最终状态。"""

    @abstractmethod
    def recent(
        self, *, limit: int = 50, host_id: str | None = None
    ) -> list[ActionExecution]:
        """按创建时间倒序查询。"""
