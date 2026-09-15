"""自动处置幂等抢占：内存实现 + SQLite 实现。"""
from __future__ import annotations

import logging
import sqlite3
import threading
from collections import OrderedDict
from datetime import datetime, timezone

from src.interfaces.action import RuleExecutionGuard
from src.storage.db import Database

logger = logging.getLogger("ops_agent.remediation_guard")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InMemoryRuleExecutionGuard(RuleExecutionGuard):
    """单进程幂等守卫；存储关闭或 SQLite 不可用时使用。"""

    def __init__(self, max_claims: int = 10000) -> None:
        self.max_claims = max(int(max_claims), 1)
        self._lock = threading.Lock()
        self._claimed: OrderedDict[tuple[str, str, str, str], None] = OrderedDict()

    def claim(
        self,
        *,
        alert_key: str,
        action: str,
        alert_status: str,
        starts_at: str,
    ) -> bool:
        key = (alert_key, action, alert_status, starts_at)
        with self._lock:
            if key in self._claimed:
                return False
            self._claimed[key] = None
            while len(self._claimed) > self.max_claims:
                self._claimed.popitem(last=False)
            return True


class SqliteRuleExecutionGuard(RuleExecutionGuard):
    """跨请求、跨重启的持久化幂等守卫。

    使用主键 + INSERT OR IGNORE 做原子抢占。数据库异常一律 fail-closed：
    返回 False，调用方不得执行写命令。
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
