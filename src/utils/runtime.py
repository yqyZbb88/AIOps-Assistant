"""单进程运行态：请求固定一个配置版本，配置应用时替换版本指针。

配置与组件属于 ``RuntimeSnapshot``；数据库连接、会话、执行幂等和最近告警等
进程级状态由新 Container 复用。这个锁只保护当前进程，不是跨 worker 租约。
读请求可在切换前绑定旧快照并继续完成；会产生副作用的业务请求先登记为
active operation，应用配置时必须等它们结束并确认没有未决审批。
"""
from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass
from typing import Any

from src.utils.config_loader import Settings


class RuntimeBusyError(RuntimeError):
    """有未决审批或正在运行的业务操作，暂不能应用配置。"""


@dataclass(frozen=True)
class RuntimeSnapshot:
    revision: str
    settings: Settings
    container: Any
    agent: Any


_request_snapshot: contextvars.ContextVar[RuntimeSnapshot | None] = contextvars.ContextVar(
    "aiops_request_snapshot", default=None
)


class RuntimeProxy:
    """兼容现有 app.state 访问方式，同时保证一次请求只读一个快照。"""

    def __init__(self, manager: "RuntimeManager", name: str) -> None:
        self._manager = manager
        self._name = name

    def __getattr__(self, name: str) -> Any:
        snapshot = _request_snapshot.get() or self._manager.current
        return getattr(getattr(snapshot, self._name), name)


class RuntimeManager:
    """协调版本切换与业务请求，不负责磁盘事务或跨进程同步。"""
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self.current = snapshot
        self._lock = threading.RLock()
        self._active_operations = 0
        self._applying = False
        self.scheduler: Any = None

    def bind_request(self, *, operation: bool = False) -> contextvars.Token | None:
        """在同一把锁下固定版本并登记业务操作。None 表示正在应用。"""
        with self._lock:
            if operation and self._applying:
                return None
            if operation:
                self._active_operations += 1
            return _request_snapshot.set(self.current)

    @staticmethod
    def reset_request(token: contextvars.Token) -> None:
        _request_snapshot.reset(token)

    def end_operation(self) -> None:
        with self._lock:
            self._active_operations -= 1

    def begin_apply(self) -> None:
        """拒绝与写业务、未决审批并发的应用；成功后由 commit/cancel_apply 收尾。"""
        with self._lock:
            if self._applying or self._active_operations:
                raise RuntimeBusyError("业务请求正在执行，请稍后重试配置应用")
            if self.current.agent.pending_approvals():
                raise RuntimeBusyError("存在未决审批，请先处理或等待过期")
            self._applying = True

    def commit(self, snapshot: RuntimeSnapshot) -> None:
        """只切换当前进程的快照；唤醒唯一调度器读取下一轮配置。"""
        with self._lock:
            self.current = snapshot
            self._applying = False
        if self.scheduler is not None:
            self.scheduler.wake()

    def cancel_apply(self) -> None:
        with self._lock:
            self._applying = False
