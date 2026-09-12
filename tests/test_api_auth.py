"""鉴权接线：逐路由 401、/health 豁免、启动快速失败、反向告警（TestClient，全离线）。"""
from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.interfaces.session import STATUS_PENDING_APPROVAL
from src.main import create_app
from src.utils.config_loader import AuthSettings, Settings, StorageSettings

_TOKENS = '{"k-alice": "alice", "k-bob": "bob"}'

# FastAPI 自己建的 app 级路由，不属于任何一个 router，本就不该被拦
_EXEMPT_PATHS = {"/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

_PARAM = re.compile(r"\{[^}]+\}")


def _as(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_app(
    *, enabled: bool = True, tokens: str = _TOKENS, db_path=None
) -> FastAPI:
    """默认不落盘（测试不写真实 db）；给了 `db_path` 才打开持久化。"""
    storage = (
        StorageSettings(enabled=False)
        if db_path is None
        else StorageSettings(enabled=True, db_path=str(db_path))
    )
    return create_app(
        settings=Settings(storage=storage, auth=AuthSettings(enabled=enabled, tokens=tokens))
    )


@pytest.fixture
def client():
    with TestClient(_make_app()) as c:
        yield c


# -- 凭据 -------------------------------------------------------------------
def test_missing_credentials_are_401(client):
    resp = client.get("/sessions")

    assert resp.status_code == 401
    # 401 必须带质询头，否则客户端 / Swagger 走不了标准流程
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_token_is_401(client):
    assert client.get("/sessions", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_non_bearer_scheme_is_401(client):
    """HTTPBearer 把 scheme 不是 Bearer 的情形也回成 None，走同一条 401 分支。"""
    resp = client.get("/sessions", headers={"Authorization": "Basic k-alice"})
    assert resp.status_code == 401


def test_valid_token_is_200(client):
    resp = client.get("/sessions", headers={"Authorization": "Bearer k-alice"})

    assert resp.status_code == 200
    assert resp.json() == []  # 持久化关闭 → 空列表（能力缺失 = 空，不是报错）


def test_error_detail_does_not_leak_the_configured_keys(client):
    """401 的 detail 是固定文案，不能把配置里的密钥回显出去。"""
    body = client.get("/sessions", headers={"Authorization": "Bearer nope"}).json()

    assert "k-alice" not in str(body)
    assert "k-bob" not in str(body)


def test_non_ascii_credential_is_401_not_500(client):
    """`compare_digest` 收 str 且含非 ASCII 会抛 TypeError → 500。

    实现里先 `.encode()` 再比对，这条从 HTTP 层再钉一次（纯逻辑那侧也有）。
    注意必须发**原始字节**：httpx 会把 str 头按 ascii 编码并直接抛 UnicodeEncodeError，
    而 ASGI 层的头本就是 bytes，服务端按 latin-1 还原——这也正是真实链路上
    非 ASCII 凭据出现的形态。
    """
    resp = client.get(
        "/sessions", headers={"Authorization": "Bearer café".encode("latin-1")}
    )

    assert resp.status_code == 401


# -- 兜底：没有任何路由漏挂 --------------------------------------------------
def test_every_route_except_exempt_requires_credentials():
    """枚举 app.routes，除豁免项外每个路由都必须拒绝**无凭据**请求。

    比逐个列举稳得多——将来新增路由会被自动覆盖，忘挂鉴权立刻变红。
    这正是选 `include_router(dependencies=...)` 而不是手工清单的理由。
    """
    app = _make_app()
    checked = 0
    with TestClient(app) as c:
        for route in app.routes:
            if not isinstance(route, APIRoute) or route.path in _EXEMPT_PATHS:
                continue
            path = _PARAM.sub("x", route.path)
            for method in sorted(route.methods):
                resp = c.request(method, path)
                assert resp.status_code == 401, (
                    f"{method} {path} 未受保护（返回 {resp.status_code}）"
                )
                checked += 1

    assert checked >= 10, f"路由枚举明显不完整（只查到 {checked} 条）"


def test_exempt_routes_do_not_require_credentials():
    """豁免的只有 /health 与 FastAPI 自建的文档路由。"""
    app = _make_app()
    with TestClient(app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/docs").status_code == 200


# -- 启动期校验 -------------------------------------------------------------
def test_enabled_without_tokens_refuses_to_start():
    """绝不接受「空密钥 = 放行」：一次配置漏填就会让服务全开。"""
    with pytest.raises(RuntimeError, match="拒绝启动"):
        _make_app(tokens="")


def test_enabled_with_invalid_tokens_refuses_to_start():
    with pytest.raises(RuntimeError, match="拒绝启动"):
        _make_app(tokens="{not json")


def test_configured_but_disabled_logs_a_warning(caplog):
    """配了密钥却没启用：不影响安全，但用户很可能**以为自己受保护了**。"""
    with caplog.at_level(logging.WARNING, logger="ops_agent.main"):
        app = _make_app(enabled=False)

    assert any("无鉴权" in r.getMessage() for r in caplog.records)

    with TestClient(app) as c:
        assert c.get("/sessions").status_code == 200, "确实没在鉴权"


# -- 关闭时行为与改造前一致 --------------------------------------------------
def test_disabled_auth_needs_no_credentials():
    """出厂默认（enabled=false）：既有调用方无需任何改动。"""
    app = _make_app(enabled=False, tokens="")
    with TestClient(app) as c:
        assert c.get("/sessions").status_code == 200
        assert c.get("/approvals").status_code == 200


# -- 会话归属：身份由密钥推出，所以隔离是真的 --------------------------------
@pytest.fixture
def owned(tmp_path):
    """鉴权开 + 持久化开的 client，外加它的 store（供直接造数据）。"""
    app = _make_app(db_path=tmp_path / "ops.db")
    with TestClient(app) as c:
        c.store = app.state.container.sessions()  # type: ignore[attr-defined]
        yield c


def _alice_session(c) -> str:
    return c.post("/sessions", json={"name": "生产排查"}, headers=_as("k-alice")).json()[
        "session_id"
    ]


def test_create_session_records_the_identity_from_the_key(owned):
    """**创建者由密钥推出**，客户端无从指定——这正是归属校验有意义的前提。"""
    sid = _alice_session(owned)

    assert owned.store.get(sid).user == "alice"


def test_list_shows_only_your_own_sessions(owned):
    _alice_session(owned)
    owned.post("/sessions", json={"name": "bob 的"}, headers=_as("k-bob"))

    alice_view = owned.get("/sessions", headers=_as("k-alice")).json()
    bob_view = owned.get("/sessions", headers=_as("k-bob")).json()

    assert [s["user"] for s in alice_view] == ["alice"]
    assert [s["user"] for s in bob_view] == ["bob"]


@pytest.mark.parametrize(
    "call",
    [
        ("get", None),
        ("delete", None),
        ("patch", {"name": "改成我的"}),
        ("post", None),  # /chat
    ],
)
def test_other_users_session_is_404(owned, call):
    """别人的会话一律 404（**不是 403**）：与「未知 id」同形，不泄露 id 是否存在。"""
    method, body = call
    sid = _alice_session(owned)
    if method == "post":
        resp = owned.post(
            "/chat", json={"message": "偷看", "session_id": sid}, headers=_as("k-bob")
        )
    elif method in ("get", "delete"):
        resp = getattr(owned, method)(f"/sessions/{sid}", headers=_as("k-bob"))
    else:
        resp = owned.patch(f"/sessions/{sid}", json=body, headers=_as("k-bob"))

    assert resp.status_code == 404


def test_owner_can_still_reach_their_own_session(owned):
    """反向：别把校验做成「谁也进不去」。"""
    sid = _alice_session(owned)

    owned.post("/chat", json={"message": "你好", "session_id": sid}, headers=_as("k-alice"))

    assert owned.get(f"/sessions/{sid}", headers=_as("k-alice")).status_code == 200
    assert owned.patch(
        f"/sessions/{sid}", json={"name": "改名了"}, headers=_as("k-alice")
    ).status_code == 200


def test_delete_by_non_owner_does_not_cancel_pending_approvals(owned):
    """**归属检查必须排在作废审批之前。**

    `cancel_pending` 不可逆（把那一轮永久标成 `abandoned`）。若先作废再发现「不是你的
    会话」，等于白废掉别人一条待执行的写命令——一次越权 DELETE 就能让别人的写命令
    永远执行不了，而攻击者拿到的只是一个 404。
    """
    sid = _alice_session(owned)
    owned.store.append_turn(
        sid, user_content="重启 nginx", assistant_content="⚠️ 待审批",
        status=STATUS_PENDING_APPROVAL, approval_id="ap-alice",
    )
    calls: list[str] = []
    owned.app.state.agent.cancel_pending = lambda aid: (calls.append(aid), True)[1]

    resp = owned.delete(f"/sessions/{sid}", headers=_as("k-bob"))

    assert resp.status_code == 404
    assert calls == [], "不得碰别人的待审批句柄"
    assert owned.store.get(sid) is not None, "会话也不能被删"
    assert owned.store.messages(sid)[1].status == STATUS_PENDING_APPROVAL


def test_legacy_row_is_invisible_until_a_key_maps_to_its_user(owned):
    """**升级须知**：开启鉴权后，存量会话的 `user` 若不在密钥映射里，就访问不到了。

    库里实测有 `user='yqyZ'` 与 `user='string'`（Swagger 示例值留下的垃圾）两类。
    要保住某段对话，把一条密钥映射到它原本的 `user` 值上即可——这条用例把那个
    逃生通道钉死，免得用户以为老数据「被删了」。
    """
    legacy = owned.store.create(user="yqyZ").session_id

    for token in ("k-alice", "k-bob"):
        assert owned.get(f"/sessions/{legacy}", headers=_as(token)).status_code == 404

    app2 = _make_app(
        tokens='{"k-yqyz": "yqyZ", "k-alice": "alice"}',
        db_path=owned.app.state.settings.storage.db_path,
    )
    with TestClient(app2) as c2:
        assert c2.get(f"/sessions/{legacy}", headers=_as("k-yqyz")).status_code == 200


def test_unowned_legacy_row_is_invisible_to_every_identity(owned):
    """`user` **为空**的存量行，任何身份都不匹配（用户选的「一律拒绝」）。"""
    unowned = owned.store.create().session_id  # user=""

    for token in ("k-alice", "k-bob"):
        assert owned.get(f"/sessions/{unowned}", headers=_as(token)).status_code == 404


def test_auth_off_sees_everything_again(owned):
    """关掉鉴权 → 无身份 → 不校验归属，行为与改造前一致（残余风险也一并回来）。"""
    sid = _alice_session(owned)

    app2 = _make_app(enabled=False, tokens="", db_path=owned.app.state.settings.storage.db_path)
    with TestClient(app2) as c2:
        assert c2.get(f"/sessions/{sid}").status_code == 200
        assert len(c2.get("/sessions").json()) == 1
