"""Multi-user host authorization, hot reload and persistence."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import json
import pytest
import yaml
from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.admin_config import AdminConfigManager
from src.utils.config_loader import StorageSettings, load_settings
from src.interfaces.approval import PendingApproval
from src.interfaces.metric import MetricSample
from tests.test_admin_config import _setup, _auth
from tests.test_admin_credentials import update, get


def manager_for(paths, settings, tmp_path):
    return AdminConfigManager(settings, settings_file=paths["settings"], hosts_file=paths["hosts"],
        rules_file=paths["rules"], actions_file=paths["actions"],
        agent_actions_file=paths["agent_actions"], diagnostics_file=paths["diagnostics"],
        backup_root=tmp_path / "backups")


@pytest.fixture
def access_app(tmp_path):
    paths, _, _ = _setup(tmp_path)
    hosts = yaml.safe_load(paths["hosts"].read_text(encoding="utf-8"))
    second = deepcopy(hosts["hosts"][0])
    second.update(id="node-b", name="Node B")
    second["prometheus"]["instance"] = "10.0.0.11:9100"
    second["ssh"]["host"] = "10.0.0.11"
    hosts["hosts"].append(second)
    paths["hosts"].write_text(yaml.safe_dump(hosts), encoding="utf-8")
    config = yaml.safe_load(paths["settings"].read_text(encoding="utf-8"))
    config["auth"].update(host_access=json.dumps({"alice": ["node-a"], "bob": ["node-b"]}))
    config["auth"]["tokens"] = json.dumps({"admin-token": "admin", "user-token": "alice",
                                          "bob-token": "bob", "alert-token": "alertmanager"})
    config["storage"] = {"enabled": True, "db_path": str(tmp_path / "sessions.db")}
    paths["settings"].write_text(yaml.safe_dump(config), encoding="utf-8")
    settings = replace(load_settings(paths["settings"]), hosts=replace(load_settings(paths["settings"]).hosts, file=str(paths["hosts"])))
    manager = manager_for(paths, settings, tmp_path)
    app = create_app(settings=settings, admin_config_manager=manager)
    app.test_paths, app.test_root = paths, tmp_path
    with TestClient(app) as client:
        yield client


def save_acl(client, grants, expected=200):
    snap = client.get("/admin/config", headers=_auth("admin-token")).json()
    config = deepcopy(snap["config"])
    config["settings"]["host_access"] = grants
    response = client.put("/admin/config", headers=_auth("admin-token"),
        json={"revision": snap["revision"], "config": config})
    assert response.status_code == expected, response.text
    return response


def create_session(client, token, host):
    response = client.post("/sessions", headers=_auth(token), json={"host_id": host})
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def test_authenticates_and_isolates_sessions(access_app):
    c = access_app
    assert c.get("/sessions").status_code == 401
    assert c.get("/hosts", headers=_auth("wrong")).status_code == 401
    sid = create_session(c, "user-token", "node-a")
    for token in ("bob-token", "admin-token"):
        headers = _auth(token)
        assert c.get(f"/sessions/{sid}", headers=headers).status_code == 404
        assert c.patch(f"/sessions/{sid}", headers=headers, json={"name": "stolen"}).status_code == 404
        assert c.delete(f"/sessions/{sid}", headers=headers).status_code == 404
        assert c.post("/chat", headers=headers, json={"session_id": sid, "message": "CPU"}).status_code == 404
        assert c.get("/sessions", headers=headers).json() == []
    assert c.get(f"/sessions/{sid}", headers=_auth("user-token")).status_code == 200
    assert c.post("/chat", headers=_auth("user-token"), json={"session_id": sid, "message": "hello"}).status_code == 200
    assert c.get("/hosts", headers=_auth("alert-token")).json() == []


@pytest.mark.parametrize("path", [
    "/hosts/node-b", "/hosts/node-b/status", "/hosts/node-b/metrics/cpu.usage",
    "/hosts/node-b/metrics/cpu.usage/trend", "/alerts/recent?host_id=node-b",
    "/alerts/active?host_id=node-b", "/alerts/history?host_id=node-b",
    "/inspections/recent?host_id=node-b", "/executions?host_id=node-b",
])
def test_direct_host_queries_cannot_bypass_acl(access_app, path):
    assert access_app.get(path, headers=_auth("user-token")).status_code == 404


def test_allowed_metric_and_unscoped_active_alerts_only_query_allowed_host(access_app, monkeypatch):
    c = access_app
    calls = []
    monitor = c.app.state.container.monitor()
    monkeypatch.setattr(monitor, "query_metric", lambda metric, **kw: [MetricSample(metric, {"device": "eth0"}, 1)])
    response = c.get("/hosts/node-a/metrics/cpu.usage", headers=_auth("user-token"))
    assert response.status_code == 200 and response.json()["samples"][0]["value"] == 1
    monkeypatch.setattr(c.app.state.agent, "active_alerts",
        lambda **kw: calls.append(kw["host_id"]) or [{"host_id": kw["host_id"]}])
    assert c.get("/alerts/active", headers=_auth("user-token")).json() == [{"host_id": "node-a"}]
    assert calls == ["node-a"]
    assert c.get("/hosts", headers=_auth("user-token")).json()[0]["host_id"] == "node-a"
    assert len(c.get("/hosts", headers=_auth("admin-token")).json()) == 2
    assert c.post("/sessions", headers=_auth("user-token"), json={"host_id": "node-b"}).status_code == 404


def test_all_history_filters_before_limit(access_app, monkeypatch):
    c = access_app
    def fetch(*, limit, host_id=None):
        rows = [{"host_id": h, "received_at": f"2026-01-0{i}", "executed_at": f"2026-01-0{i}",
                 "analyzed_at": f"2026-01-0{i}", "checked_at": f"2026-01-0{i}", "created_at": f"2026-01-0{i}"}
                for i, h in enumerate(["node-a", "node-b", "node-b"], 1)]
        return [SimpleNamespace(to_dict=lambda row=row: row) for row in reversed(rows)
                if host_id is None or row["host_id"] == host_id][:limit]
    history = c.app.state.container.history()
    for method in ("recent_alerts", "recent_advice", "recent_remediations"):
        monkeypatch.setattr(history, method, fetch)
    monkeypatch.setattr(c.app.state.container.inspection_store(), "recent", fetch)
    monkeypatch.setattr(c.app.state.container.execution_store(), "recent", fetch)
    monkeypatch.setattr(c.app.state.agent, "recent", lambda **kw: [r.to_dict() for r in fetch(**kw)])
    for path, keys in [("/alerts/history", ["alerts", "advice", "remediations"]),
                       ("/inspections/recent", ["inspections"]), ("/executions", ["executions"])]:
        result = c.get(path + "?limit=1", headers=_auth("user-token")).json()
        for key in keys:
            assert len(result[key]) == 1 and result[key][0]["host_id"] == "node-a"
    assert c.get("/alerts/recent?limit=1", headers=_auth("user-token")).json()[0]["host_id"] == "node-a"


def test_revoke_blocks_existing_session_and_keeps_history(access_app):
    c = access_app
    sid = create_session(c, "user-token", "node-a")
    save_acl(c, {"alice": [], "bob": ["node-b"]})
    assert c.get("/hosts", headers=_auth("user-token")).json() == []
    assert c.get("/sessions", headers=_auth("user-token")).json() == []
    assert c.get(f"/sessions/{sid}", headers=_auth("user-token")).status_code == 404
    assert c.post("/chat", headers=_auth("user-token"), json={"session_id": sid, "message": "CPU"}).status_code == 404
    assert c.app.state.container.sessions().get(sid) is not None
    save_acl(c, {"alice": ["node-a"], "bob": ["node-b"]})
    assert c.get(f"/sessions/{sid}", headers=_auth("user-token")).status_code == 200


def test_managed_token_rotation_revocation_and_restart_preserve_identity_acl(access_app):
    c = access_app
    created = update(c, "tokens", get(c).json()["revision"], {"identity": "carol"}, "POST").json()
    first = created["token_once"]
    assert c.get("/hosts", headers=_auth(first)).json() == []
    save_acl(c, {"alice": ["node-a"], "bob": ["node-b"], "carol": ["node-a"]})
    sid = create_session(c, first, "node-a")
    rotated = update(c, "tokens/" + created["token_id"] + "/rotate", get(c).json()["revision"], method="POST").json()
    second = rotated["token_once"]
    assert c.get("/hosts", headers=_auth(first)).status_code == 401
    assert c.get(f"/sessions/{sid}", headers=_auth(second)).status_code == 200
    paths = c.app.test_paths
    settings = replace(load_settings(paths["settings"]), hosts=replace(c.app.state.settings.hosts, file=str(paths["hosts"])))
    manager = manager_for(paths, settings, c.app.test_root)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as restarted:
        assert restarted.get(f"/sessions/{sid}", headers=_auth(second)).status_code == 200
        assert [h["host_id"] for h in restarted.get("/hosts", headers=_auth(second)).json()] == ["node-a"]
        revoked = update(restarted, "tokens/" + created["token_id"], get(restarted).json()["revision"], method="DELETE")
        assert revoked.status_code == 200
        assert restarted.get(f"/sessions/{sid}", headers=_auth(second)).status_code == 401
        assert restarted.app.state.container.sessions().get(sid).user == "carol"


def test_pending_blocks_config_and_checks_both_participants(access_app, monkeypatch):
    c = access_app
    pending = PendingApproval("p1", "now", user="alice", host_id="node-a")
    monkeypatch.setattr(c.app.state.runtime.current.agent, "pending_approvals", lambda **kw: [pending])
    decisions = []
    monkeypatch.setattr(c.app.state.runtime.current.agent, "decide", lambda *a, **kw: decisions.append(a))
    save_acl(c, {}, expected=409)
    # ACL changes normally require an empty pending queue. Simulate stale recovered state as defense in depth.
    c.app.state.settings.auth.host_access  # frozen snapshot; use an explicit pending requester without access
    pending = replace(pending, user="bob")
    assert c.get("/approvals", headers=_auth("admin-token")).json() == []
    assert c.post("/approvals/p1", headers=_auth("admin-token"), json={"action": "approve"}).status_code == 404
    pending = replace(pending, user="alice")
    assert len(c.get("/approvals", headers=_auth("admin-token")).json()) == 1
    assert c.get("/approvals", headers=_auth("bob-token")).json() == []
    assert c.post("/approvals/p1", headers=_auth("bob-token"), json={"action": "approve"}).status_code == 404
    assert decisions == []


@pytest.mark.parametrize("grants", [{"alice": ["unknown"]}, {"alice": ["*"]}, {"alice": "node-a"}, ["node-a"]])
def test_invalid_grants_rollback(access_app, grants):
    c = access_app
    before = c.app.state.admin_config.revision()
    save_acl(c, grants, expected=422)
    assert c.app.state.admin_config.revision() == before
    assert c.get("/hosts", headers=_auth("user-token")).json()[0]["host_id"] == "node-a"


def test_non_admin_cannot_change_grants(access_app):
    snap = access_app.get("/admin/config", headers=_auth("admin-token")).json()
    response = access_app.put("/admin/config", headers=_auth("user-token"),
        json={"revision": snap["revision"], "config": snap["config"]})
    assert response.status_code == 403


def test_session_host_filter_precedes_limit(access_app):
    c = access_app
    visible = create_session(c, "user-token", "node-a")
    hidden = c.app.state.container.sessions().create(user="alice", host_id="node-b")
    rows = c.get("/sessions?limit=1", headers=_auth("user-token")).json()
    assert [row["session_id"] for row in rows] == [visible]
    assert c.get("/sessions/" + hidden.session_id, headers=_auth("user-token")).status_code == 404
