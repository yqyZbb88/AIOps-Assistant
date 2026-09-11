"""待审批项的内存缓冲（ApprovalStore）。

Agent 写命令被 HumanInTheLoopMiddleware 中断后，需要一个地方把
「审批 id → 线程 id + 摘要」暂存起来，等人工经 /approvals 提交决策时取出续跑。
本模块就是那个暂存处（进程重启即清空，与 AlertStore 同一约定）。

设计要点：
- **单次性**：pop() 取出即删。审批 id 一旦被消费就不能再用——防 HTTP 层重放导致
  同一条写命令被执行两次（LangGraph 本身对已完成线程的重复 resume 不会重复执行，
  但显式单次性让边界更清楚，且能给出准确的 404 而非静默成功）。
- **有界**：超出 maxlen 按最旧淘汰，淘汰的线程 id 会被调用方 delete_thread（避免
  LangGraph 的 InMemorySaver 无限累积）。淘汰只丢弃，**绝不放行**。
- **会过期**：超时即作废（fail-safe：宁可让人重新发起，也不能拿一个陈旧的中断去执行
  写命令）。purge_expired() 返回被清理的记录，调用方据此删线程。
- **不存凭据**：只存审批 id / 线程 id / 命令摘要 / 时间。连接信息在插件实例里，
  与本模块无关。

clock 可注入，便于离线单测过期逻辑（默认取系统时间）。
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("ops_agent.approval_store")

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
        self._records: dict[str, dict[str, Any]] = {}
        self._order: deque[str] = deque()

    # -- 写入 --------------------------------------------------------------
    def add(self, record: dict[str, Any]) -> dict[str, Any] | None:
        """存入一条待审批记录（须含 approval_id）；超容量时返回**被淘汰的整条记录**，否则 None。

        返回被淘汰记录是为了让调用方 `delete_thread` —— 不返回就等于泄漏 checkpoint 线程。
        **必须返回完整记录而非只有 approval_id**：调用方要的是 `thread_id`，
        只给 id 等于没给（2026-09-11 修：原先只回 `{"approval_id": …}`，调用方拿到后
        无从删除线程，`_delete_thread(None)` 静默无操作，泄漏照旧发生）。
        """
        approval_id = record["approval_id"]
        record.setdefault("created_at", _now_iso())
        record["_expires_at"] = self._clock() + self.ttl_seconds

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
        rec = self._records.get(approval_id)
        if rec is None:
            return None
        if rec.get("_expires_at", 0) <= self._clock():
            self._drop(approval_id)
            logger.info("待审批项已过期，作废: %s（该写命令不会执行）", approval_id)
            return None
        return rec

    def pop(self, approval_id: str) -> dict[str, Any] | None:
        """取出并删除（**单次性**）；不存在/已过期返回 None。"""
        rec = self.get(approval_id)
        if rec is None:
            return None
        self._drop(approval_id)
        return rec

    def list(self) -> list[dict[str, Any]]:
        """当前待审批记录，新的在前（顺带清理已过期的）。"""
        self.purge_expired()
        return [self._records[i] for i in reversed(self._order) if i in self._records]

    def purge_expired(self) -> list[dict[str, Any]]:
        """清理所有已过期记录，返回被清理的记录（供调用方 delete_thread）。"""
        now = self._clock()
        expired = [r for r in self._records.values() if r.get("_expires_at", 0) <= now]
        for rec in expired:
            self._drop(rec["approval_id"])
            logger.info("待审批项已过期，作废: %s（该写命令不会执行）", rec["approval_id"])
        return expired

    # -- 内部 --------------------------------------------------------------
    def _drop(self, approval_id: str) -> None:
        self._records.pop(approval_id, None)
        try:
            self._order.remove(approval_id)
        except ValueError:
            pass

    def __len__(self) -> int:
        return len(self._records)
