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
    assert resp.json() == {"session_id": session_id, "deleted": True}
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
