"""审批端点：GET /approvals、POST /approvals/{id}（TestClient，全离线）。

用真 create_app（不触发 Agent 构建——构建只在首次聊天发生），
再把假 assistant 注入 ChatService 的 `_assistant`，从而绕开 langchain 与网络。
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from src.interfaces.approval import ApprovalError, ApprovalOutcome, PendingApproval
from src.main import create_app
from src.utils.config_loader import AuthSettings, Settings
from src.utils.logger import AUDIT_LOGGER_NAME

_TOKENS = '{"k-alice": "alice", "k-bob": "bob"}'


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class FakeApprovalAssistant:
    """实现 Assistant + ApprovalCapable 的假对象。"""

    def __init__(self, pending_user: str = "") -> None:
        self.decisions: list[tuple[str, str, str]] = []
        self.decision_actors: list[str | None] = []
        self._pending: list[PendingApproval] = [
            PendingApproval(
                approval_id="abc123",
                created_at="2026-01-01T00:00:00+00:00",
                actions=({"name": "restart_service", "args": {"service": "nginx"}},),
                user=pending_user,
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

    def decide_as(
        self,
        approval_id: str,
        *,
        action: str,
        reason: str = "",
        decided_by: str | None = None,
    ) -> ApprovalOutcome:
        self.decision_actors.append(decided_by)
        return self.decide(approval_id, action=action, reason=reason)


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
    assert client.fake.decision_actors == [None]


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


def test_chat_response_shape(client):
    """/chat **响应体**三键不变；变的是请求体（2026-09-12 破坏性变更）。

    请求体删掉了 `user` 与 `session_name`、`session_id` 改为必填——身份改由密钥推出，
    建会话拆到了 `POST /sessions`。响应里 `session_name` 保留，含义从「客户端给的别名」
    变成「该会话的标签（只读回带）」。

    这里持久化是关的（`client` 用 `Settings()`），`session_id` 传什么都行：
    存储不可用时会被忽略，`reply` 照常走关键词/假 Agent。
    """
    resp = client.post("/chat", json={"message": "你好", "session_id": "whatever"})

    assert resp.status_code == 200
    assert set(resp.json()) == {"reply", "session_id", "session_name"}
    assert isinstance(resp.json()["reply"], str)


def test_chat_without_session_id_is_422(client):
    """必填字段缺失 → 422，而不是静默新建一段会话。"""
    assert client.post("/chat", json={"message": "你好"}).status_code == 422


# -- 归属隔离（鉴权开启时） --------------------------------------------------
def _auth_app(pending_user: str):
    """鉴权开着的 client，待审批项归属 `pending_user`。"""
    app = create_app(settings=Settings(auth=AuthSettings(enabled=True, tokens=_TOKENS)))
    app.state.agent.chat_service._assistant = FakeApprovalAssistant(pending_user=pending_user)
    return app


def test_list_approvals_only_shows_your_own():
    with TestClient(_auth_app(pending_user="alice")) as c:
        assert [p["approval_id"] for p in c.get("/approvals", headers=_as("k-alice")).json()] == [
            "abc123"
        ]
        assert c.get("/approvals", headers=_as("k-bob")).json() == [], "别人的待审批项不可见"


def test_list_approvals_exposes_the_owner():
    """`user` 回带出来——运维排查「这条是谁触发的」要靠它。"""
    with TestClient(_auth_app(pending_user="alice")) as c:
        assert c.get("/approvals", headers=_as("k-alice")).json()[0]["user"] == "alice"


def test_decide_by_non_owner_is_404():
    """**别人批不动**——这是 `/approvals` 补上的那一层授权。

    404 而不是 403：与「未知 / 已过期 / 已处理」同形，调用者分不出
    「这条不存在」与「这条不是你的」。
    """
    with TestClient(_auth_app(pending_user="alice")) as c:
        resp = c.post("/approvals/abc123", json={"action": "approve"}, headers=_as("k-bob"))

    assert resp.status_code == 404


def test_decide_by_non_owner_does_not_touch_the_queue():
    """越权尝试不得有任何副作用——队列还在，原始助手一次决策都没收到。"""
    app = _auth_app(pending_user="alice")
    with TestClient(app) as c:
        c.post("/approvals/abc123", json={"action": "approve"}, headers=_as("k-bob"))

        assert len(c.get("/approvals", headers=_as("k-alice")).json()) == 1
        assert app.state.agent.chat_service._assistant.decisions == []


def test_decide_by_owner_works():
    with TestClient(_auth_app(pending_user="alice")) as c:
        resp = c.post("/approvals/abc123", json={"action": "approve"}, headers=_as("k-alice"))

    assert resp.status_code == 200
    assert resp.json()["decision"] == "approve"


def test_unowned_pending_is_invisible_to_everyone():
    """鉴权关闭时产生的待审批项（`user` 为空）→ 开启鉴权后**谁都批不了**。

    后果是它一直挂到 TTL 淘汰、**命令永不执行**——这正是 fail-safe 要的方向。
    （实践中这条路径很难触发：待审批项存在进程内存里，不跨重启存活，
    所以「关着鉴权产生的项」在开启鉴权那一刻已经随重启消失了。这里只是把规则钉死。）
    """
    with TestClient(_auth_app(pending_user="")) as c:
        assert c.get("/approvals", headers=_as("k-alice")).json() == []
        assert c.post(
            "/approvals/abc123", json={"action": "approve"}, headers=_as("k-alice")
        ).status_code == 404


def test_auth_off_sees_and_decides_any_pending():
    """鉴权关闭 → 不校验归属，行为与改造前完全一致。"""
    app = create_app(settings=Settings())
    app.state.agent.chat_service._assistant = FakeApprovalAssistant(pending_user="someone")

    with TestClient(app) as c:
        assert len(c.get("/approvals").json()) == 1
        assert c.post("/approvals/abc123", json={"action": "approve"}).status_code == 200


def test_unknown_id_is_404_even_when_owned_by_someone_else():
    """队列里根本没有这个 id → 仍是 404（与归属不匹配同形）。"""
    with TestClient(_auth_app(pending_user="alice")) as c:
        assert c.post(
            "/approvals/nope", json={"action": "approve"}, headers=_as("k-alice")
        ).status_code == 404


def _approvers_only_app(pending_user: str = "alice"):
    app = create_app(
        settings=Settings(
            auth=AuthSettings(
                enabled=True,
                tokens=_TOKENS,
                approval_mode="approvers_only",
                approver_identities='["bob"]',
            )
        )
    )
    app.state.agent.chat_service._assistant = FakeApprovalAssistant(pending_user=pending_user)
    return app


def test_approvers_only_blocks_requester_and_allows_approver():
    app = _approvers_only_app()
    with TestClient(app) as c:
        assert c.get("/approvals", headers=_as("k-alice")).json() == []
        assert c.get("/approvals", headers=_as("k-bob")).json()[0]["approval_id"] == "abc123"
        assert c.post(
            "/approvals/abc123",
            json={"action": "approve"},
            headers=_as("k-alice"),
        ).status_code == 404
        resp = c.post(
            "/approvals/abc123",
            json={"action": "approve"},
            headers=_as("k-bob"),
        )

    assert resp.status_code == 200
    assert app.state.agent.chat_service._assistant.decision_actors == ["bob"]


# -- 决策人审计 --------------------------------------------------------------
def _audit_lines(caplog) -> list[dict]:
    return [
        json.loads(r.getMessage())
        for r in caplog.records
        if r.name == AUDIT_LOGGER_NAME
    ]


def test_decision_is_audited_with_the_decider(caplog):
    """**谁批准的**必须进审计——这是事后追责时唯一能回答它的地方。

    注意 `caplog.set_level` 的位置：必须在 `create_app` **之后**。`configure_logging`
    会把审计 logger 的级别重置成 `NOTSET`（跟随根级别，默认 WARNING），
    在那之前设的级别会被冲掉，记录在 logger 那一层就被丢了。
    """
    with TestClient(_auth_app(pending_user="alice")) as c:
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        c.post(
            "/approvals/abc123",
            json={"action": "approve", "reason": "高峰期已过"},
            headers=_as("k-alice"),
        )

    lines = _audit_lines(caplog)
    assert len(lines) == 1
    assert lines[0]["action"] == "approval_decision"
    assert lines[0]["decided_by"] == "alice"
    assert lines[0]["decision"] == "approve"
    assert lines[0]["approval_id"] == "abc123"
    assert lines[0]["reason"] == "高峰期已过"
    assert lines[0]["commands"] == ["restart_service"], "记下这条待审批包含哪些命令"
    assert lines[0]["decided_at"]


def test_reject_is_audited_too(caplog):
    """拒绝也要留痕——「谁否掉了这条写命令」同样是审计要回答的。"""
    with TestClient(_auth_app(pending_user="alice")) as c:
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        c.post("/approvals/abc123", json={"action": "reject"}, headers=_as("k-alice"))

    assert _audit_lines(caplog)[0]["decision"] == "reject"


def test_unknown_id_leaves_no_audit_line(caplog):
    """队列里没有的 id（打错 / 探测）**不该在审计里留下一串「批准」的假记录**。"""
    with TestClient(_auth_app(pending_user="alice")) as c:
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        assert c.post(
            "/approvals/nope", json={"action": "approve"}, headers=_as("k-alice")
        ).status_code == 404

    assert _audit_lines(caplog) == []


def test_non_owner_leaves_no_audit_line(caplog):
    """越权尝试在归属校验那一步就被 404 挡下，不该记成「bob 批准了」。"""
    with TestClient(_auth_app(pending_user="alice")) as c:
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        c.post("/approvals/abc123", json={"action": "approve"}, headers=_as("k-bob"))

    assert _audit_lines(caplog) == []


def test_decided_by_is_empty_when_auth_is_off(caplog):
    """鉴权关闭时没有身份可言——审计里留空串，而不是编一个假的。

    「当时有没有鉴权」本身就是审计要回答的问题之一。
    """
    app = create_app(settings=Settings())
    app.state.agent.chat_service._assistant = FakeApprovalAssistant(pending_user="")
    with TestClient(app) as c:
        caplog.set_level(logging.INFO, logger=AUDIT_LOGGER_NAME)
        c.post("/approvals/abc123", json={"action": "approve"})

    assert _audit_lines(caplog)[0]["decided_by"] == ""


def test_approvals_routes_registered():
    app = create_app(settings=Settings())
    paths = {r.path for r in app.routes}
    assert "/approvals" in paths
    assert "/approvals/{approval_id}" in paths



def test_approval_waits_for_published_turn_then_can_resume(client, tmp_path):
    from src.interfaces.session import STATUS_PENDING_APPROVAL
    from src.storage.db import open_database
    from src.storage.session import SqliteSessionStore
    store = SqliteSessionStore(open_database(tmp_path / "sessions.db"))
    client.app.state.agent.chat_service._sessions = store
    session = store.create()
    client.fake._pending = [PendingApproval("abc123", "test-time", session_id=session.session_id)]
    response = client.post("/approvals/abc123", json={"action": "approve"})
    assert response.status_code == 503
    assert client.fake.decisions == []
    store.append_turn(session.session_id, user_content="test-write", assistant_content="test-pending",
                      status=STATUS_PENDING_APPROVAL, approval_id="abc123")
    response = client.post("/approvals/abc123", json={"action": "approve"})
    assert response.status_code == 200
    assert len(client.fake.decisions) == 1
