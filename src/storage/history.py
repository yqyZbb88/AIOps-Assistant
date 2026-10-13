"""SQLite 告警、处置和 AI 建议的批量归档及运维历史清理。

除 `_TABLES` 的三张归档表，本模块按同一历史策略清理 remediation_claims、
approval_decisions、approval_requests 和 action_executions。相关写入分别由对应 Store
负责；清理须保护 pending 申请及仍被待审批消息引用的决策。

契约见 src/interfaces/history.py::HistoryStore。要点：

- **一次 `record_batch` = 一个事务**：三张表同批落，不留「告警写了、处置没写」的半批；
- **写入前强制截断** `detail` / `advice` / `summary`，控制外部文本和命令输出造成的库增长；
  截断不是脱敏，调用方仍不得把凭据或其它敏感内容放入这些字段；
- 告警表用 `UNIQUE(alert_key, starts_at, status)` + `INSERT OR IGNORE` 做**重投幂等**，
  同时保留 firing → resolved 的状态迁移（Alertmanager 会重投，但不要
  `UNIQUE(alert_key)` + REPLACE——那会让「曾经 firing 过」的痕迹消失）；
- 清理（TTL + 容量）惰性 + 节流触发，不引入后台线程（同会话存储）。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Callable, Sequence

from src.interfaces.history import (
    MAX_ADVICE_CHARS,
    MAX_DETAIL_CHARS,
    MAX_SUMMARY_CHARS,
    AdviceRecord,
    AlertRecord,
    HistoryError,
    HistoryStore,
    RemediationRecord,
)
from src.storage.db import Database
from src.utils.time import cutoff_iso
from src.utils.security import redact_text

logger = logging.getLogger("aiops_assistant.storage.history")

DEFAULT_TTL_SECONDS = 168 * 3600  # 7 天
DEFAULT_MAX_ROWS = 5000
DEFAULT_PURGE_INTERVAL_SECONDS = 60

# 三张表 + 各自的时间列（清理与排序都用它）
_TABLES: dict[str, str] = {
    "alerts": "received_at",
    "remediations": "executed_at",
    "ai_advice": "analyzed_at",
}

_INSERT_ALERT = (
    "INSERT OR IGNORE INTO alerts "
    "(alert_key, alertname, severity, instance, summary, status, starts_at, ends_at, "
    "received_at, host_id, host_resolution) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_REMEDIATION = (
    "INSERT INTO remediations "
    "(alert_key, alertname, severity, instance, matched_rule, action, status, detail, "
    "executed_at, host_id, execution_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_ADVICE = (
    "INSERT INTO ai_advice "
    "(alert_key, alertname, severity, instance, advice, analyzed_at, host_id) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

_SELECT_COLUMNS: dict[str, str] = {
    "alerts": "alert_key, alertname, severity, instance, summary, status, "
              "starts_at, ends_at, received_at, host_id, host_resolution",
    "remediations": "alert_key, alertname, severity, instance, matched_rule, action, "
                    "status, detail, executed_at, host_id, execution_id",
    "ai_advice": "alert_key, alertname, severity, instance, advice, analyzed_at, host_id",
}

_BUILDERS = {
    "alerts": AlertRecord,
    "remediations": RemediationRecord,
    "ai_advice": AdviceRecord,
}


def _clip(text: str, limit: int) -> str:
    """截断超长文本（见模块 docstring 的安全说明）。"""
    text = redact_text(text or "")
    if not text or len(text) <= limit:
        return text or ""
    return text[:limit] + "…（已截断）"


class SqliteHistoryStore(HistoryStore):
    """状态日志的 SQLite 实现。"""

    def __init__(
        self,
        db: Database,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_rows: int = DEFAULT_MAX_ROWS,
        clock: Callable[[], float] | None = None,
        purge_interval_seconds: float = DEFAULT_PURGE_INTERVAL_SECONDS,
    ) -> None:
        self.db = db
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self.max_rows = max(int(max_rows), 1)
        self.purge_interval_seconds = max(float(purge_interval_seconds), 0.0)
        self._clock: Callable[[], float] = clock or time.time
        self._healthy = True
        self._last_error: str | None = None
        self._last_purge = 0.0

    # -- 状态 ---------------------------------------------------------------
    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _fail(self, exc: Exception) -> HistoryError:
        self._healthy = False
        self._last_error = str(exc)
        logger.error("状态日志存储操作失败: %s", exc, exc_info=True)
        return HistoryError(str(exc))

    def _ok(self) -> None:
        """一次成功操作 → 标记健康。

        `healthy` 反映「最近一次操作的结果」，不是「历史上有没有失败过」——
        否则一次几秒的瞬时锁就会让 `/health` 永远显示 degraded，而库其实好好的，
        那个字段也就没人信了（与 SqliteSessionStore 同款处理）。
        """
        self._healthy = True
        self._last_error = None

    # -- 写入 ---------------------------------------------------------------
    def record_batch(
        self,
        *,
        alerts: Sequence[AlertRecord] = (),
        remediations: Sequence[RemediationRecord] = (),
        advice: Sequence[AdviceRecord] = (),
    ) -> None:
        """三张表同事务写入（一次调用 = 一个 BEGIN IMMEDIATE）。"""
        if not alerts and not remediations and not advice:
            return
        try:
            with self.db.transaction() as conn:
                for rec in alerts:
                    conn.execute(_INSERT_ALERT, (
                        rec.alert_key, rec.alertname, rec.severity, rec.instance,
                        _clip(rec.summary, MAX_SUMMARY_CHARS), rec.status,
                        rec.starts_at, rec.ends_at, rec.received_at,
                        rec.host_id, rec.host_resolution,
                    ))
                for rec in remediations:
                    conn.execute(_INSERT_REMEDIATION, (
                        rec.alert_key, rec.alertname, rec.severity, rec.instance,
                        rec.matched_rule, rec.action, rec.status,
                        _clip(rec.detail, MAX_DETAIL_CHARS), rec.executed_at,
                        rec.host_id, rec.execution_id,
                    ))
                for rec in advice:
                    conn.execute(_INSERT_ADVICE, (
                        rec.alert_key, rec.alertname, rec.severity, rec.instance,
                        _clip(rec.advice, MAX_ADVICE_CHARS), rec.analyzed_at, rec.host_id,
                    ))
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()

        self._maybe_purge()

    # -- 读取 ---------------------------------------------------------------
    def recent_alerts(
        self, *, limit: int = 50, host_id: str | None = None
    ) -> list[AlertRecord]:
        return self._recent("alerts", limit, host_id)

    def recent_remediations(
        self, *, limit: int = 50, host_id: str | None = None
    ) -> list[RemediationRecord]:
        return self._recent("remediations", limit, host_id)

    def recent_advice(
        self, *, limit: int = 50, host_id: str | None = None
    ) -> list[AdviceRecord]:
        return self._recent("ai_advice", limit, host_id)

    def _recent(self, table: str, limit: int, host_id: str | None) -> list[Any]:
        time_col = _TABLES[table]
        where = "WHERE host_id = ? " if host_id is not None else ""
        sql = (
            f"SELECT {_SELECT_COLUMNS[table]} FROM {table} "
            f"{where}"
            f"ORDER BY {time_col} DESC, id DESC LIMIT ?"  # id 次键：同秒写入的顺序才稳定
        )
        params = (host_id, max(1, int(limit))) if host_id is not None else (max(1, int(limit)),)
        try:
            rows = self.db.query(sql, params)
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()
        builder = _BUILDERS[table]
        return [builder(**row) for row in rows]

    def count(self, table: str) -> int:
        """某张表的行数（供测试与运维查看）。"""
        if table not in _TABLES:
            raise ValueError(f"未知的归档表: {table}")
        try:
            n = int(self.db.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()
        return n

    # -- 清理 ---------------------------------------------------------------
    def purge_expired(self) -> int:
        """按 TTL/容量清理归档、幂等、审批及执行历史，保留仍待处理的申请/决策证据。"""
        cutoff = cutoff_iso(self._clock(), self.ttl_seconds)
        removed = 0
        try:
            with self.db.transaction() as conn:
                for table, time_col in _TABLES.items():
                    removed += conn.execute(
                        f"DELETE FROM {table} WHERE {time_col} < ?", (cutoff,)
                    ).rowcount
                    # 容量上限：保留最新的 max_rows 行
                    removed += conn.execute(
                        f"DELETE FROM {table} WHERE id NOT IN "
                        f"(SELECT id FROM {table} ORDER BY {time_col} DESC, id DESC LIMIT ?)",
                        (self.max_rows,),
                    ).rowcount
                removed += conn.execute(
                    "DELETE FROM remediation_claims WHERE claimed_at < ?", (cutoff,)
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM remediation_claims WHERE rowid NOT IN "
                    "(SELECT rowid FROM remediation_claims ORDER BY claimed_at DESC, rowid DESC LIMIT ?)",
                    (self.max_rows,),
                ).rowcount
                # pending 轮仍依赖决策记录判断是否可能执行过，不能按 TTL/容量先删证据。
                # 对账或正常完成后状态不再是 pending，下次清理恢复原保留策略。
                # 会话删除也会在自己的事务中删除决策；session_id 为空的记录由这里兜底。
                protect_pending = (
                    " AND NOT EXISTS (SELECT 1 FROM messages "
                    "WHERE messages.approval_id = approval_decisions.approval_id "
                    "AND messages.status = 'pending_approval')"
                )
                removed += conn.execute(
                    "DELETE FROM approval_decisions WHERE decided_at < ?" + protect_pending,
                    (cutoff,),
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM approval_decisions WHERE rowid NOT IN "
                    "(SELECT rowid FROM approval_decisions "
                    "ORDER BY decided_at DESC, rowid DESC LIMIT ?)" + protect_pending,
                    (self.max_rows,),
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM action_executions WHERE created_at < ?", (cutoff,)
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM approval_requests WHERE status != 'pending' AND created_at < ?", (cutoff,)
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM approval_requests WHERE status != 'pending' AND rowid NOT IN "
                    "(SELECT rowid FROM approval_requests ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                    (self.max_rows,),
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM action_executions WHERE rowid NOT IN "
                    "(SELECT rowid FROM action_executions "
                    "ORDER BY created_at DESC, rowid DESC LIMIT ?)",
                    (self.max_rows,),
                ).rowcount
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()

        self._last_purge = self._clock()
        if removed:
            # WAL 只增不减，删行不会让它缩小；低频截断一次（事务外，非 WAL 下是空操作）
            self.db.checkpoint()
        return removed

    def _maybe_purge(self) -> None:
        """按节流间隔触发清理。清理失败不影响本轮写入（写入已成功）。"""
        if self._clock() - self._last_purge < self.purge_interval_seconds:
            return
        try:
            self.purge_expired()
        except HistoryError as exc:
            logger.warning("状态日志清理失败（不影响本轮写入）: %s", exc)
