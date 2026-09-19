"""统一动作执行审计的 SQLite 实现。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from src.interfaces.execution import ActionExecution, ActionExecutionStore
from src.storage.db import Database
from src.utils.security import redact_sensitive, redact_text
from src.utils.time import now_iso


class SqliteActionExecutionStore(ActionExecutionStore):
    def __init__(self, db: Database) -> None:
        self.db = db

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
        execution_id = uuid.uuid4().hex
        stamp = now_iso()
        started_at = stamp if status == "running" else ""
        finished_at = stamp if status in {"succeeded", "failed", "rejected", "skipped"} else ""
        payload = json.dumps(
            redact_sensitive(params), ensure_ascii=False, separators=(",", ":")
        )
        try:
            changed = self.db.execute(
                "INSERT OR IGNORE INTO action_executions "
                "(execution_id, channel, idempotency_key, host_id, action_name, "
                "params_json, target_summary, config_fingerprint, approval_id, alert_key, "
                "status, detail, created_at, started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    execution_id, channel, idempotency_key, host_id, action_name,
                    payload, target_summary, config_fingerprint, approval_id, alert_key,
                    status, redact_text(detail), stamp, started_at, finished_at,
                ),
            )
        except sqlite3.Error:
            raise
        if not changed:
            return None
        return ActionExecution(
            execution_id=execution_id,
            channel=channel,
            idempotency_key=idempotency_key,
            host_id=host_id,
            action_name=action_name,
            params=redact_sensitive(params),
            target_summary=target_summary,
            config_fingerprint=config_fingerprint,
            approval_id=approval_id,
            alert_key=alert_key,
            status=status,
            detail=redact_text(detail),
            created_at=stamp,
            started_at=started_at,
            finished_at=finished_at,
        )

    def finish(self, execution_id: str, *, status: str, detail: str = "") -> None:
        self.db.execute(
            "UPDATE action_executions SET status = ?, detail = ?, finished_at = ? "
            "WHERE execution_id = ?",
            (status, redact_text(detail)[:500], now_iso(), execution_id),
        )

    def recent(
        self, *, limit: int = 50, host_id: str | None = None
    ) -> list[ActionExecution]:
        where = "WHERE host_id = ? " if host_id is not None else ""
        params = (host_id, max(1, int(limit))) if host_id is not None else (max(1, int(limit)),)
        rows = self.db.query(
            "SELECT execution_id, channel, idempotency_key, host_id, action_name, "
            "params_json, target_summary, config_fingerprint, approval_id, alert_key, "
            "status, detail, created_at, started_at, finished_at "
            f"FROM action_executions {where}ORDER BY created_at DESC, rowid DESC LIMIT ?",
            params,
        )
        result: list[ActionExecution] = []
        for row in rows:
            raw = row.pop("params_json")
            try:
                values = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                values = {}
            result.append(ActionExecution(params=values, **row))
        return result
