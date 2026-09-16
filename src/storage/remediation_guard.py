"""自动处置幂等抢占：内存实现 + SQLite 实现。"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable

from src.interfaces.action import RuleExecutionGuard
from src.storage.db import Database
# 内存版的保留期与 SQLite 版**同源同值**：remediation_claims 的过期由状态日志清理按
# history_ttl_hours 执行（见 storage/history.py 的 purge_expired），这里直接沿用它的默认值。
from src.storage.history import DEFAULT_TTL_SECONDS as DEFAULT_CLAIM_TTL_SECONDS

logger = logging.getLogger("aiops_assistant.remediation_guard")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InMemoryRuleExecutionGuard(RuleExecutionGuard):
    """单进程幂等守卫；存储关闭或 SQLite 不可用时使用。

    **必须与 SQLite 版同一个保留期**：两套实现是同一种能力（RuleExecutionGuard）的
    两种落点，而 history_ttl_hours 管的正是「同一条告警的同一个动作还要不要拦」这种
    安全相关的窗口。内存版若永不落定过期，配置在**降级路径**上就静默失效了——
    降级时行为变得更宽松（或更严格），恰恰是最不该悄悄发生的事。

    取舍：过期后同一条告警（同 `starts_at`）再次投递会被重新执行一次。这是
    「有界内存 > 绝对幂等」的选择，与 max_claims 淘汰同一性质——SQLite 版亦然
    （行被 TTL 或容量上限删掉后，重投就拦不住了）。
    """

    def __init__(
        self,
        max_claims: int = 10000,
        *,
        ttl_seconds: float = DEFAULT_CLAIM_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.max_claims = max(int(max_claims), 1)
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        # 只看时间差，故用单调时钟：系统时间被回调不会让一批抢占记录集体提前过期
        self._clock: Callable[[], float] = clock or time.monotonic
        self._lock = threading.Lock()
        #: key → 抢占时刻。OrderedDict 的插入序即时间序（时钟单调），故头部恒为最旧。
        self._claimed: OrderedDict[tuple[str, str, str, str], float] = OrderedDict()

    def _expire(self, now: float) -> None:
        """丢掉已过保留期的抢占记录（调用方须持锁）。"""
        while self._claimed:
            claimed_at = next(iter(self._claimed.values()))
            if now - claimed_at < self.ttl_seconds:
                break
            self._claimed.popitem(last=False)

    def claim(
        self,
        *,
        alert_key: str,
        action: str,
        alert_status: str,
        starts_at: str,
    ) -> bool:
        key = (alert_key, action, alert_status, starts_at)
        now = self._clock()
        with self._lock:
            self._expire(now)
            if key in self._claimed:
                return False
            self._claimed[key] = now
            while len(self._claimed) > self.max_claims:
                self._claimed.popitem(last=False)
            return True


class SqliteRuleExecutionGuard(RuleExecutionGuard):
    """跨请求、跨重启的持久化幂等守卫。

    使用主键 + INSERT OR IGNORE 做原子抢占。数据库异常一律 fail-closed：
    返回 False，调用方不得执行写命令。

    记录**不是永久**的：由状态日志清理按 `history_ttl_hours`（默认 7 天）+ 容量上限
    过期（见 storage/history.py 的 purge_expired）。内存版沿用同一个默认保留期
    （见 InMemoryRuleExecutionGuard）。
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def claim(
        self,
        *,
        alert_key: str,
        action: str,
        alert_status: str,
        starts_at: str,
    ) -> bool:
        try:
            rowcount = self.db.execute(
                "INSERT OR IGNORE INTO remediation_claims "
                "(alert_key, action, alert_status, starts_at, claimed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (alert_key, action, alert_status, starts_at, _now_iso()),
            )
        except sqlite3.Error as exc:
            logger.error("自动处置幂等抢占失败，拒绝执行以保安全: %s", exc, exc_info=True)
            return False
        return rowcount == 1
