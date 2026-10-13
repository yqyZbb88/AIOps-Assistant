"""审批进行中可独立读取决策/执行事实，保持角色和主机隔离。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from src.interfaces.approval import ApprovalOutcome, PendingApproval
from tests.test_admin_config import _auth
from tests.test_host_access import access_app, save_acl


def test_progress_during_delayed_response_uses_committed_facts(access_app):
    c = access_app
    store = c.app.state.container.sessions()
    session = store.create(user="alice", host_id="node-a")
    record = {"approval_id": "slow-ai", "session_id": session.session_id, "user": "alice", "host_id": "node-a", "created_at": "2026-10-13T00:00:00+00:00"}
    store.record_request(record)
    store.append_turn(session.session_id, user_content="request", assistant_content="pending", status="pending_approval", approval_id="slow-ai")
    executed, release = Event(), Event()
    class SlowAssistant:
        pending = True
        def pending_approvals(self):
            return [PendingApproval(approval_id="slow-ai", created_at=record["created_at"], user="alice", host_id="node-a", session_id=session.session_id)] if self.pending else []
        def decide(self, approval_id, *, action, reason=""):
            self.pending = False
            store.record_decision(approval_id, session_id=session.session_id, action=action, decided_by="admin", host_id="node-a")
            c.app.state.container.execution_store().create(channel="agent", idempotency_key="slow-ai", host_id="node-a",
                action_name="restart", params={}, approval_id=approval_id, status="succeeded")
            executed.set()
            assert release.wait(10), "test did not release synthetic AI"
            return ApprovalOutcome(approval_id, action, "completed", "synthetic reply")
    c.app.state.agent.chat_service._assistant = SlowAssistant()
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(c.post, "/approvals/slow-ai", headers=_auth("admin-token"), json={"action": "approve"})
        try:
            assert executed.wait(5)
            assert not request.done()
            response = c.get("/approvals/slow-ai/progress", headers=_auth("admin-token"))
            assert response.status_code == 200, response.text
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["status"] == "approve"
            assert response.json()["executions"][0]["status"] == "succeeded"
            assert c.get("/approvals/slow-ai/progress", headers=_auth("bob-token")).status_code == 404
            assert c.get("/approvals/slow-ai/progress", headers=_auth("user-token")).status_code == 404
            assert c.get("/approvals/slow-ai/progress").status_code == 401
        finally:
            release.set()
        assert request.result().status_code == 200
    save_acl(c, {"alice": [], "bob": ["node-b"]})
    assert c.get("/approvals/slow-ai/progress", headers=_auth("admin-token")).status_code == 404
