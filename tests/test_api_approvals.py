"""审批端点：GET /approvals、POST /approvals/{id}（TestClient，全离线）。

用真 create_app（不触发 Agent 构建——构建只在首次聊天发生），
再把假 assistant 注入 ChatService 的 `_assistant`，从而绕开 langchain 与网络。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.interfaces.approval import ApprovalError, ApprovalOutcome, PendingApproval
from src.main import create_app
from src.utils.config_loader import Settings


class FakeApprovalAssistant:
    """实现 Assistant + ApprovalCapable 的假对象。"""

    def __init__(self) -> None:
        self.decisions: list[tuple[str, str, str]] = []
        self._pending: list[PendingApproval] = [
            PendingApproval(
                approval_id="abc123",
                created_at="2026-01-01T00:00:00+00:00",
                actions=({"name": "restart_service", "args": {"service": "nginx"}},),
            )
        ]

    def reply(self, message: str) -> str:
        return f"[Agent]{message}"

    def pending_approvals(self) -> list[PendingApproval]:
        return list(self._pending)

    def decide(self, approval_id: str, *, action: str, reason: str = "") -> ApprovalOutcome:
        self.decisions.append((approval_id, action, reason))
        if approval_id != "abc123":
            raise ApprovalError(f"未知、已过期或已被处理的审批 id: {approval_id}")
        if action == "reject":
            return ApprovalOutcome(approval_id, action, "completed", "已取消该操作")
        self._pending = []
        return ApprovalOutcome(approval_id, action, "completed", "已重启 nginx")


@pytest.fixture
def client() -> TestClient:
    app = create_app(settings=Settings())
    fake = FakeApprovalAssistant()
    # 注入假 assistant：绕过 langchain/网络，同时保住真实路由与 OpsAgent 转发链路
    app.state.agent.chat_service._assistant = fake
    with TestClient(app) as c:
        c.fake = fake  # type: ignore[attr-defined]
    return c


def test_list_approvals(client):
    resp = client.get("/approvals")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["approval_id"] == "abc123"
    assert body[0]["actions"][0]["name"] == "restart_service"
    assert body[0]["allowed_decisions"] == ["approve", "reject"]


def test_approve_resumes(client):
    resp = client.post("/approvals/abc123", json={"action": "approve"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "approve"
    assert body["status"] == "completed"
    assert body["next_approval_id"] is None
    assert client.fake.decisions == [("abc123", "approve", "")]


def test_reject_passes_through(client):
    resp = client.post("/approvals/abc123", json={"action": "reject", "reason": "高峰期"})
    assert resp.status_code == 200
    assert resp.json()["decision"] == "reject"
    assert client.fake.decisions == [("abc123", "reject", "高峰期")]


def test_edit_decision_is_422(client):
    """edit 之类的值必须在入参校验阶段就被拒——绝不能到达 resume（会毒化线程）。"""
    resp = client.post("/approvals/abc123", json={"action": "edit"})
    assert resp.status_code == 422
    assert client.fake.decisions == [], "非法决策不得触达 assistant"


def test_missing_action_is_422(client):
    assert client.post("/approvals/abc123", json={}).status_code == 422


def test_unknown_id_is_404(client):
    assert client.post("/approvals/nope", json={"action": "approve"}).status_code == 404


def test_unavailable_agent_is_503():
    """未启用写命令能力时：列表为空，提交决策 503（功能没开 ≠ 找错 id）。"""
    app = create_app(settings=Settings())
    # _assistant 保持 None 且工厂会失败（无 langchain/无 key）→ 审批不可用
    app.state.agent.chat_service._agent_broken = True
    with TestClient(app) as c:
        assert c.get("/approvals").json() == []
        assert c.post("/approvals/x", json={"action": "approve"}).status_code == 503


def test_chat_response_shape_unchanged(client):
    """/chat 契约零改动：仍恰好返回 {"reply": str}。"""
    resp = client.post("/chat", json={"user": "ops", "message": "你好"})
    assert resp.status_code == 200
    assert set(resp.json()) == {"reply"}
    assert isinstance(resp.json()["reply"], str)


def test_approvals_routes_registered():
    app = create_app(settings=Settings())
    paths = {r.path for r in app.routes}
    assert "/approvals" in paths
    assert "/approvals/{approval_id}" in paths
