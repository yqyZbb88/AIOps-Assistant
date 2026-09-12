"""鉴权接线：逐路由 401、/health 豁免、启动快速失败、反向告警（TestClient，全离线）。"""
from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import AuthSettings, Settings, StorageSettings

_TOKENS = '{"k-alice": "alice", "k-bob": "bob"}'

# FastAPI 自己建的 app 级路由，不属于任何一个 router，本就不该被拦
_EXEMPT_PATHS = {"/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

_PARAM = re.compile(r"\{[^}]+\}")


def _make_app(*, enabled: bool = True, tokens: str = _TOKENS) -> FastAPI:
    return create_app(
        settings=Settings(
            storage=StorageSettings(enabled=False),  # 不落盘：测试不写真实 db
            auth=AuthSettings(enabled=enabled, tokens=tokens),
        )
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
