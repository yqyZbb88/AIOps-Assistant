"""审批追溯：冻结申请、旧决策/消息投影与独立执行结果；不推测旧归属。"""
import json

from src.utils.security import redact_sensitive, redact_text


class ApprovalHistory:
    def __init__(self, db):
        self.db = db

    def record_request(self, record):
        self.db.execute(
            "INSERT INTO approval_requests (approval_id, session_id, requester, host_id, "
            "target_summary, actions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (record["approval_id"], record.get("session_id"), record.get("user") or "",
             record.get("host_id") or "", redact_text(record.get("target_summary") or ""),
             json.dumps(redact_sensitive(record.get("actions") or []), ensure_ascii=False), record["created_at"]),
        )

    def terminal(self, approval_id, status):
        # 过期/撤销没有人工审批人和审批时间，不能把系统清理时间当审批时间。
        self.db.execute("UPDATE approval_requests SET status = ? WHERE approval_id = ? AND status = 'pending'",
                        (status, approval_id))

    def host_ids(self):
        """仅供管理员取得已删除登记的历史主机范围，不作为普通用户的授权来源。"""
        return {r["host_id"] for r in self.db.query(
            "SELECT host_id FROM approval_requests WHERE host_id != '' UNION "
            "SELECT host_id FROM approval_decisions WHERE host_id != ''")}

    def progress(self, approval_id):
        """单条审批的已提交事实；不根据 HTTP 请求是否完成推测执行结果。"""
        rows = self.db.query(
            "SELECT approval_id, requester, host_id, status, decided_by, decided_at "
            "FROM approval_requests WHERE approval_id = ?", (approval_id,))
        if not rows:
            # 兼容升级前的未决申请：只从决策和会话关联获取可信归属。
            rows = self.db.query(
                "SELECT d.approval_id, COALESCE(s.user, '') AS requester, d.host_id, "
                "d.action AS status, d.decided_by, d.decided_at "
                "FROM approval_decisions d LEFT JOIN sessions s ON s.session_id = d.session_id "
                "WHERE d.approval_id = ? ORDER BY d.decided_at DESC LIMIT 1", (approval_id,))
        if not rows:
            return None
        result = rows[0]
        result["executions"] = self.db.query(
            "SELECT execution_id, action_name, status, started_at, finished_at "
            "FROM action_executions WHERE approval_id = ? AND host_id = ? ORDER BY created_at, rowid",
            (approval_id, result["host_id"]))
        return result

    def page(self, *, identity, hosts, approver, status=None, limit=25, offset=0):
        if not hosts:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        # v7 及之前只在可证明的 session/message 关联中获取申请人/时间。
        # abandoned 只证明已作废，无法区分重启、过期、撤销，不猜测原因。
        source = """
          SELECT r.approval_id, r.session_id, r.requester, r.host_id, r.target_summary,
                 r.actions_json, r.created_at, r.status, r.decided_by, r.decided_at
            FROM approval_requests r
          UNION ALL
          SELECT d.approval_id, d.session_id, COALESCE(s.user, ''), d.host_id,
                 d.target_summary, d.actions_json,
                 COALESCE((SELECT MIN(m.created_at) FROM messages m WHERE m.approval_id = d.approval_id), ''),
                 d.action, d.decided_by, d.decided_at
            FROM approval_decisions d LEFT JOIN sessions s ON s.session_id = d.session_id
           WHERE NOT EXISTS (SELECT 1 FROM approval_requests r WHERE r.approval_id = d.approval_id)
          UNION ALL
          SELECT m.approval_id, m.session_id, s.user, s.host_id, '', '[]', MIN(m.created_at),
                 CASE WHEN m.status = 'abandoned' THEN 'abandoned' ELSE 'unknown' END, '', ''
            FROM messages m JOIN sessions s ON s.session_id = m.session_id
           WHERE m.role = 'assistant' AND m.approval_id IS NOT NULL AND m.status IN ('abandoned', 'unknown')
             AND NOT EXISTS (SELECT 1 FROM approval_requests r WHERE r.approval_id = m.approval_id)
             AND NOT EXISTS (SELECT 1 FROM approval_decisions d WHERE d.approval_id = m.approval_id)
           GROUP BY m.approval_id
        """
        where = "host_id IN (" + ",".join("?" for _ in hosts) + ") AND status != 'pending'"
        args = tuple(sorted(hosts))
        if not approver:
            where += " AND requester = ?"
            args += (identity,)
        if status:
            where += " AND status = ?"
            args += (status,)
        total = self.db.query(f"SELECT COUNT(*) AS n FROM ({source}) WHERE {where}", args)[0]["n"]
        rows = self.db.query(f"SELECT * FROM ({source}) WHERE {where} "
                             "ORDER BY COALESCE(NULLIF(decided_at, ''), created_at) DESC, approval_id DESC LIMIT ? OFFSET ?",
                             (*args, limit, offset))
        for row in rows:
            try:
                actions = json.loads(row.pop("actions_json"))
                row["actions"] = redact_sensitive([a for a in actions if isinstance(a, dict)]) if isinstance(actions, list) else []
            except (ValueError, TypeError):
                row["actions"] = []
            # 同一申请可能有多个执行动作，逐条保留结果，不汇成虚假的成功。
            row["executions"] = self.db.query(
                "SELECT execution_id, action_name, status, started_at, finished_at "
                "FROM action_executions WHERE approval_id = ? AND host_id = ? ORDER BY created_at, rowid",
                (row["approval_id"], row["host_id"]))
        return {"items": rows, "total": total, "limit": limit, "offset": offset}
