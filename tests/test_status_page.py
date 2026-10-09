"""旧状态页已移除；图形化控制台是唯一浏览器入口。"""
from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.config_loader import AuthSettings, Settings


def test_status_routes_are_removed_without_redirect():
    settings = Settings(auth=AuthSettings(tokens='{"secret-token":"admin"}'))
    with TestClient(create_app(settings=settings)) as client:
        page = client.get("/status")
        css = client.get("/status-assets/status.css")
        js = client.get("/status-assets/status.js")
    assert page.status_code == 404
    assert css.status_code == 404
    assert js.status_code == 404
    assert "location" not in page.headers
