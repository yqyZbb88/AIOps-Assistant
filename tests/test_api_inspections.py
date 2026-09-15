"""巡检归档查询接口：鉴权、分页、存储降级。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.interfaces.inspection import (
    LEVEL_WARNING,
    InspectionError,
    InspectionFinding,
    InspectionResult,
)
from src.main import create_app
from src.utils.config_loader import (
    AuthSettings,
    InspectionSettings,
    Settings,
    StorageSettings,
)

_TOKENS = '{"k-ops": "ops"}'


def _as(token: str = "k-ops") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _settings(tmp_path, *, auth=True, storage=True):
    return Settings(
        auth=AuthSettings(enabled=auth, tokens=_TOKENS if auth else ""),
        storage=(
            StorageSettings(enabled=True, db_path=str(tmp_path / "ops.db"))
            if storage
            else StorageSettings(enabled=False)
        ),
        inspection=InspectionSettings(enabled=False),
    )


def _result() -> InspectionResult:
    from datetime import datetime, timezone

    return InspectionResult(
        checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        level=LEVEL_WARNING,
        snapshot={"cpu": {"enabled": True, "value": 85.0, "level": LEVEL_WARNING}},
        findings=(InspectionFinding("cpu", LEVEL_WARNING, "cpu high"),),
    )


def test_recent_requires_auth(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        assert client.get("/inspections/recent").status_code == 401
        assert client.get("/inspections/recent", headers=_as("bad")).status_code == 401


def test_recent_returns_abnormal_archive(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    app.state.container.inspection_store().record(_result())
    with TestClient(app) as client:
        resp = client.get("/inspections/recent", headers=_as())

    assert resp.status_code == 200
    body = resp.json()
    assert body["storage"] == "ok"
    assert body["inspections"][0]["level"] == LEVEL_WARNING
    assert body["inspections"][0]["findings"][0]["message"] == "cpu high"


def test_recent_rejects_invalid_limit(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path))) as client:
        assert client.get("/inspections/recent?limit=0", headers=_as()).status_code == 422
        assert client.get("/inspections/recent?limit=501", headers=_as()).status_code == 422


def test_recent_returns_off_when_storage_disabled(tmp_path):
    with TestClient(create_app(settings=_settings(tmp_path, storage=False))) as client:
        resp = client.get("/inspections/recent", headers=_as())

    assert resp.status_code == 200
    assert resp.json() == {"storage": "off", "inspections": []}


def test_recent_storage_error_is_503(tmp_path):
    app = create_app(settings=_settings(tmp_path))
    store = app.state.container.inspection_store()

    def boom(*args, **kwargs):
        raise InspectionError("db broken")

    store.recent = boom  # type: ignore[method-assign]
    with TestClient(app) as client:
        resp = client.get("/inspections/recent", headers=_as())

    assert resp.status_code == 503
    assert "db broken" in resp.json()["detail"]
