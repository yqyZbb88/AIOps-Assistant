"""会话端点：GET /sessions、GET /sessions/{id}、DELETE /sessions/{id}（TestClient，全离线）。

用真 create_app + 真 SQLite（落在 tmp_path），不触发 Agent 构建、不联网。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import Settings, StorageSettings


def _settings(tmp_path, *, enabled: bool = True) -> Settings:
    return Settings(
        storage=StorageSettings(enabled=enabled, db_path=str(tmp_path / "ops.db")),
    )


@pytest.fixture
def client(tmp_path) -> TestClient:
    app = create_app(settings=_settings(tmp_path))
    with TestClient(app) as c:
        c.store = app.state.container.sessions()  # type: ignore[attr-defined]
    return c


@pytest.fixture
def disabled_client(tmp_path) -> TestClient:
    """持久化未启用：模拟出厂默认 / 用户显式关闭。"""
    app = create_app(settings=_settings(tmp_path, enabled=False))
    with TestClient(app) as c:
        c.path = tmp_path / "ops.db"  # type: ignore[attr-defined]
    return c


def _seed(store) -> str:
    info = store.create(user="ops")
    store.append_turn(info.session_id, user_content="CPU 怎么样", assistant_content="45%")
    return info.session_id


# -- 正常路径 ---------------------------------------------------------------
def test_list_sessions(client):
    session_id = _seed(client.store)

    resp = client.get("/sessions")

    assert resp.status_code == 200
    body = resp.json()
    assert [s["session_id"] for s in body] == [session_id]
    assert body[0]["message_count"] == 2


def test_list_sessions_respects_limit(client):
    for _ in range(3):
        _seed(client.store)

    assert len(client.get("/sessions", params={"limit": 2}).json()) == 2


def test_list_sessions_rejects_bad_limit(client):
    assert client.get("/sessions", params={"limit": 0}).status_code == 422


def test_get_session_returns_metadata_and_messages(client):
    session_id = _seed(client.store)

    resp = client.get(f"/sessions/{session_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == session_id
    assert body["user"] == "ops"
    assert body["title"] == "CPU 怎么样"
    assert [(m["role"], m["content"]) for m in body["messages"]] == [
        ("user", "CPU 怎么样"),
        ("assistant", "45%"),
    ]


def test_get_session_shows_pending_messages(client):
    """展示口要看到 pending 行（与给模型看的窗口不同）。"""
    info = client.store.create()
    client.store.append_turn(
        info.session_id, user_content="重启 nginx", assistant_content="⚠️ 待审批",
        status="pending_approval", approval_id="ap1",
    )

    body = client.get(f"/sessions/{info.session_id}").json()

    assert body["pending_approvals"] == 1
    assert [m["status"] for m in body["messages"]] == ["pending_approval", "pending_approval"]


def test_get_unknown_session_is_404(client):
    assert client.get("/sessions/nope").status_code == 404


def test_delete_session(client):
    session_id = _seed(client.store)

    resp = client.delete(f"/sessions/{session_id}")

    assert resp.status_code == 200
    assert resp.json() == {
        "session_id": session_id, "deleted": True, "cancelled_approvals": 0
    }
    assert client.get(f"/sessions/{session_id}").status_code == 404


def test_delete_twice_is_404(client):
    """删过的 id 再来一次是 404——**不静默重建**，否则 DELETE 形同虚设。"""
    session_id = _seed(client.store)
    client.delete(f"/sessions/{session_id}")

    assert client.delete(f"/sessions/{session_id}").status_code == 404


# -- 持久化未启用 -----------------------------------------------------------
def test_list_returns_empty_when_disabled(disabled_client):
    """沿用 GET /approvals 的约定：能力缺失 = 空列表，而非报错。"""
    resp = disabled_client.get("/sessions")

    assert resp.status_code == 200
    assert resp.json() == []


def test_detail_is_503_when_disabled(disabled_client):
    assert disabled_client.get("/sessions/whatever").status_code == 503


def test_delete_is_503_when_disabled(disabled_client):
    assert disabled_client.delete("/sessions/whatever").status_code == 503


def test_disabled_does_not_create_db_file(disabled_client):
    """关闭时不得留下任何 db 文件（哪怕是空库）。"""
    disabled_client.get("/sessions")
    assert not disabled_client.path.exists()


# -- /chat 多轮（M4 第二批接线） --------------------------------------------
@pytest.fixture
def chat_client(tmp_path):
    """一个装了「支持历史」的假 Agent 的 client，用于验证 /chat 的会话接线。"""
    from tests.test_chat_history import FakeHistoryAssistant

    app = create_app(settings=_settings(tmp_path))
    agent = FakeHistoryAssistant()
    app.state.agent.chat_service._assistant = agent
    with TestClient(app) as c:
        c.agent = agent  # type: ignore[attr-defined]
        c.store = app.state.container.sessions()  # type: ignore[attr-defined]
    return c


def test_chat_creates_session_and_returns_id(chat_client):
    resp = chat_client.post("/chat", json={"user": "ops", "message": "CPU 怎么样"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"reply", "session_id"}
    assert body["session_id"], "不传 session_id 时应新建并在响应里回带"
    assert [s["session_id"] for s in chat_client.get("/sessions").json()] == [body["session_id"]]


def test_chat_with_session_id_is_multi_turn(chat_client):
    first = chat_client.post("/chat", json={"user": "ops", "message": "第一问"}).json()

    second = chat_client.post(
        "/chat", json={"user": "ops", "message": "第二问", "session_id": first["session_id"]}
    ).json()

    assert second["session_id"] == first["session_id"], "同一会话不应换 id"
    assert [m.content for m in chat_client.agent.seen[1]] == ["第一问", "[多轮]第一问"]
    detail = chat_client.get(f"/sessions/{first['session_id']}").json()
    assert [m["content"] for m in detail["messages"]] == [
        "第一问", "[多轮]第一问", "第二问", "[多轮]第二问"
    ]


def test_chat_without_session_id_starts_new_each_time(chat_client):
    a = chat_client.post("/chat", json={"user": "ops", "message": "问一"}).json()
    b = chat_client.post("/chat", json={"user": "ops", "message": "问二"}).json()

    assert a["session_id"] != b["session_id"]
    assert len(chat_client.get("/sessions").json()) == 2


def test_chat_with_unknown_session_id_is_404(chat_client):
    resp = chat_client.post(
        "/chat", json={"user": "ops", "message": "你好", "session_id": "ghost"}
    )

    assert resp.status_code == 404, "未知 id 不得静默重建（否则 DELETE 形同虚设）"


def test_chat_works_when_storage_disabled(disabled_client):
    """持久化关掉时 /chat 照常回答，只是 session_id 为 null。"""
    from tests.test_chat_history import FakeHistoryAssistant

    disabled_client.app.state.agent.chat_service._assistant = FakeHistoryAssistant()
    resp = disabled_client.post("/chat", json={"user": "ops", "message": "你好"})

    assert resp.status_code == 200
    assert resp.json()["reply"] == "[多轮]你好"
    assert resp.json()["session_id"] is None


# -- 数据真的落盘（跨 Container 实例可读） ------------------------------------
def test_data_survives_new_container(tmp_path):
    """换一个 app 实例仍能读到——这正是「持久化」与内存缓冲的区别。"""
    app1 = create_app(settings=_settings(tmp_path))
    with TestClient(app1) as c1:
        session_id = _seed(app1.state.container.sessions())

    app2 = create_app(settings=_settings(tmp_path))
    with TestClient(app2) as c2:
        resp = c2.get(f"/sessions/{session_id}")

    assert resp.status_code == 200
    assert resp.json()["message_count"] == 2
