"""状态页只提供无数据空壳，真实数据仍受 Bearer 鉴权。"""
from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import AuthSettings, Settings


def test_status_shell_is_public_but_contains_no_operational_data():
    settings = Settings(auth=AuthSettings(enabled=True, tokens='{"secret-token":"admin"}'))
    with TestClient(create_app(settings=settings)) as client:
        page = client.get("/status")
        protected = client.get("/alerts/history")
    assert page.status_code == 200
    assert "secret-token" not in page.text
    assert "sessionStorage" not in page.text
    assert protected.status_code == 401
    assert page.headers["cache-control"] == "no-store"
    assert "default-src 'self'" in page.headers["content-security-policy"]


def test_status_assets_are_local_and_public():
    with TestClient(create_app(settings=Settings())) as client:
        css = client.get("/status-assets/status.css")
        js = client.get("/status-assets/status.js")
    assert css.status_code == 200
    assert js.status_code == 200
    assert "sessionStorage" in js.text
    assert "innerHTML" not in js.text
