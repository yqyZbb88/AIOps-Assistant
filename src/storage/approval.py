"""待审批项的有界进程内缓冲，保存审批 ID、线程 ID 与冻结摘要，不存凭据。

加锁原子消费避免重复续跑；仅恢复执行前的决策落库失败可由调用方重新入队。
容量/过期清理返回移除对象，供调用方释放 checkpoint 并收敛会话。重启后不保留。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("aiops_assistant.approval_store")

DEFAULT_MAX_PENDING = 32
DEFAULT_TTL_SECONDS = 900


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalStore:
    """待审批项的有界内存缓冲（新的在后，读取时新的在前）。"""

    def __init__(
        self,
        maxlen: int = DEFAULT_MAX_PENDING,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.maxlen = max(maxlen, 1)
        self.ttl_seconds = max(int(ttl_seconds), 1)
        self._clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
        # 所有公开操作共用一把可重入锁。pop 必须是原子的：get() + _drop()
        # 若让并发请求同时穿过，同一个 approval_id 会被返回两次并重复续跑写命令。
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque()

    # -- 写入 --------------------------------------------------------------
    def add(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """存入含 approval_id 的记录，返回容量淘汰对象，否则 None。

        调用方依靠返回的 thread_id 释放 checkpoint；淘汰不放行执行。
        """
        approval_id = record["approval_id"]
        record.setdefault("created_at", _now_iso())
        record["_expires_at"] = self._clock() + self.ttl_seconds

        with self._lock:
            evicted: dict[str, Any] | None = None
            while len(self._order) >= self.maxlen:
                oldest = self._order.popleft()
                dropped = self._records.pop(oldest, None)
                logger.warning("待审批队列已满，淘汰最旧的一条: %s（该写命令不会执行）", oldest)
                if evicted is None:
                    # 兜底：记录已被别处移除时至少给出 id，避免返回 None 让调用方以为没淘汰
                    evicted = dropped if dropped is not None else {"approval_id": oldest}
            self._records[approval_id] = record
            self._order.append(approval_id)
            return evicted

    # -- 读取 --------------------------------------------------------------
    def get(self, approval_id: str) -> dict[str, Any] | None:
        """按 id 取记录（不删除）；不存在或已过期返回 None。"""
        with self._lock:
            rec = self._records.get(approval_id)
            if rec is None:
                return None
            if rec.get("_expires_at", 0) <= self._clock():
                self._drop_locked(approval_id)
                logger.info("待审批项已过期，作废: %s（该写命令不会执行）", approval_id)
                return None
            return rec

    def pop(self, approval_id: str) -> dict[str, Any] | None:
        """取出并删除（**单次性**）；不存在/已过期返回 None。"""
        with self._lock:
            rec = self._records.get(approval_id)
            if rec is None:
                return None
            if rec.get("_expires_at", 0) <= self._clock():
                self._drop_locked(approval_id)
                logger.info("待审批项已过期，作废: %s（该写命令不会执行）", approval_id)
                return None
            self._drop_locked(approval_id)
            return rec

    def remove(self, approval_id: str) -> dict[str, Any] | None:
        """无条件删除并返回完整记录，未知 ID 返回 None。

        不走 get 的过期判定，以便会话删除时也能取到过期记录的 thread_id 进行清理。
        """
        # 直接查 _records 而**不走 get()**：get() 对已过期的一律返回 None，
        # 那样过期项既删不掉、调用方也拿不到 thread_id 去清线程。这里要的就是「无论如何删掉它」。
        with self._lock:
            record = self._records.get(approval_id)
            if record is None:
                return None
            self._drop_locked(approval_id)
            return record

    def list(self) -> list[dict[str, Any]]:
        """当前待审批记录，新的在前（顺带清理已过期的）。"""
        with self._lock:
            self._purge_expired_locked()
            return [self._records[i] for i in reversed(self._order) if i in self._records]

    def purge_expired(self) -> list[dict[str, Any]]:
        """清理所有已过期记录，返回被清理的记录（供调用方 delete_thread）。"""
        with self._lock:
            return self._purge_expired_locked()

    # -- 内部 --------------------------------------------------------------
    def _purge_expired_locked(self) -> list[dict[str, Any]]:
        """已持锁时清理过期项；调用方不得在未持锁状态下调用。"""
        now = self._clock()
        expired = [r for r in self._records.values() if r.get("_expires_at", 0) <= now]
        for rec in expired:
            self._drop_locked(rec["approval_id"])
            logger.info("待审批项已过期，作废: %s（该写命令不会执行）", rec["approval_id"])
        return expired

    def _drop_locked(self, approval_id: str) -> None:
        """已持锁时删除记录与顺序项。"""
        self._records.pop(approval_id, None)
        try:
            self._order.remove(approval_id)
        except ValueError:
            pass

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)
