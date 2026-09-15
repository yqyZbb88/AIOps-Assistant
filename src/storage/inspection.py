"""固定指标巡检的 SQLite 归档实现。"""
from __future__ import annotations

import json
import logging
import sqlite3
import time

from src.interfaces.inspection import (
    LEVEL_NORMAL,
    InspectionError,
    InspectionFinding,
    InspectionResult,
    InspectionStore,
)
from src.storage.db import Database
from src.utils.time import cutoff_iso

logger = logging.getLogger("ops_agent.storage.inspection")


class SqliteInspectionStore(InspectionStore):
    """仅存放异常巡检轮次；TTL + 最大行数限制库增长。"""

    def __init__(
        self,
        db: Database,
        *,
        ttl_seconds: float,
        max_rows: int,
        clock=None,
    ) -> None:
        self.db = db
        self.ttl_seconds = max(float(ttl_seconds), 1.0)
        self.max_rows = max(int(max_rows), 1)
        self._clock = clock or time.time
        self._healthy = True
        self._last_error: str | None = None

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def _fail(self, exc: Exception) -> InspectionError:
        self._healthy = False
        self._last_error = str(exc)
        logger.error("巡检归档操作失败: %s", exc, exc_info=True)
        return InspectionError(str(exc))

    def _ok(self) -> None:
        self._healthy = True
        self._last_error = None

    def record(self, result: InspectionResult) -> None:
        if result.level == LEVEL_NORMAL:
            return
        try:
            self.db.execute(
                "INSERT INTO inspection_runs "
                "(checked_at, level, snapshot_json, findings_json, error_text) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    result.checked_at,
                    result.level,
                    json.dumps(result.snapshot, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(
                        [finding.to_dict() for finding in result.findings],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    result.error,
                ),
            )
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()
        try:
            self.purge_expired()
        except InspectionError as exc:
            logger.warning("巡检归档清理失败（不影响已写入记录）: %s", exc)

    def recent(self, *, limit: int = 50) -> list[InspectionResult]:
        try:
            rows = self.db.query(
                "SELECT id, checked_at, level, snapshot_json, findings_json, error_text "
                "FROM inspection_runs ORDER BY checked_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            )
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()

        results: list[InspectionResult] = []
        for row in rows:
            try:
                snapshot = json.loads(row["snapshot_json"])
                raw_findings = json.loads(row["findings_json"])
                findings = tuple(InspectionFinding(**item) for item in raw_findings)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                logger.error("巡检归档 JSON 损坏 id=%s: %s", row["id"], exc)
                continue
            results.append(
                InspectionResult(
                    record_id=int(row["id"]),
                    checked_at=row["checked_at"],
                    level=row["level"],
                    snapshot=snapshot,
                    findings=findings,
                    error=row["error_text"] or "",
                )
            )
        return results

    def purge_expired(self) -> int:
        cutoff = cutoff_iso(self._clock(), self.ttl_seconds)
        try:
            with self.db.transaction() as conn:
                removed = conn.execute(
                    "DELETE FROM inspection_runs WHERE checked_at < ?", (cutoff,)
                ).rowcount
                removed += conn.execute(
                    "DELETE FROM inspection_runs WHERE id NOT IN "
                    "(SELECT id FROM inspection_runs ORDER BY checked_at DESC, id DESC LIMIT ?)",
                    (self.max_rows,),
                ).rowcount
        except sqlite3.Error as exc:
            raise self._fail(exc) from exc
        self._ok()
        if removed:
            self.db.checkpoint()
        return removed
