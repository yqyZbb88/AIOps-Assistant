"""身份、删除、凭据编辑与审批追溯的隔离验收，不连接真实服务。"""
import sqlite3

import pytest
import yaml
from fastapi.testclient import TestClient

from src.main import create_app
from src.storage.approval_history import ApprovalHistory
from src.storage.db import open_database
from src.storage.session import SqliteSessionStore
from src.utils.host_onboarding import HostOnboarding
from tests.test_admin_config import _auth, _setup
from tests.test_host_access import access_app, save_acl


def test_identity_is_server_fact_with_actual_roles(access_app):
    c = access_app
    assert c.get("/identity").status_code == 401
    admin = c.get("/identity", headers=_auth("admin-token"))
    assert admin.headers["cache-control"] == "no-store"
    assert admin.json()["identity"] == "admin"
    assert set(admin.json()["roles"]) == {"user", "admin", "approver"}
    assert "admin-token" not in admin.text
    alice = c.get("/identity", headers=_auth("user-token")).json()
    assert alice["identity"] == "alice" and alice["roles"] == ["user"]


@pytest.mark.parametrize("stop", [False, True])
def test_managed_delete_syncs_only_when_requested(tmp_path, monkeypatch, stop):
    paths, settings, manager = _setup(tmp_path, target_mode="managed")
    monkeypatch.setattr(HostOnboarding, "_targets", lambda self: [])
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as c:
        rev = manager.revision()
        added = c.post("/admin/hosts/add", headers=_auth("admin-token"), json={"revision": rev,
            "draft": {"host_id": "node-c", "name": "C", "exporter_address": "fixture:9100"}})
        assert added.status_code == 200, added.text
        rev = manager.revision()
        before = manager.target_path.read_bytes()
        assert c.request("DELETE", "/admin/hosts/node-c", headers=_auth("user-token"), json={"revision": rev}).status_code == 403
        assert c.request("DELETE", "/admin/hosts/node-c", headers=_auth("admin-token"), json={"revision": "0" * 64}).status_code == 409
        result = c.request("DELETE", "/admin/hosts/node-c", headers=_auth("admin-token"), json={"revision": rev, "stop_collection": stop})
        assert result.status_code == 200, result.text
        assert manager.runtime.current.container.hosts().get("node-c") is None
        assert (yaml.safe_load(manager.target_path.read_text()) == []) if stop else manager.target_path.read_bytes() == before
        if not stop:
            stopped = c.request("DELETE", "/admin/targets", headers=_auth("admin-token"),
                                json={"revision": manager.revision(), "address": "fixture:9100"})
            assert stopped.status_code == 200, stopped.text
            assert yaml.safe_load(manager.target_path.read_text()) == []


def test_delete_references_running_operations_and_external_boundary(access_app):
    c = access_app
    manager = c.app.state.admin_config
    def delete(stop=False):
        return c.request("DELETE", "/admin/hosts/node-a", headers=_auth("admin-token"),
                         json={"revision": manager.revision(), "stop_collection": stop})
    rev = manager.revision()
    assert delete().status_code == 409  # 用户授权仍引用
    assert manager.revision() == rev
    save_acl(c, {"alice": [], "bob": ["node-b"]})
    assert delete().status_code == 409  # 禁用规则也必须清理引用
    snap = c.get("/admin/config", headers=_auth("admin-token")).json()
    snap["config"]["rules"] = []
    assert c.put("/admin/config", headers=_auth("admin-token"), json={"revision": snap["revision"], "config": snap["config"]}).status_code == 200
    assert delete(True).status_code == 422  # 外部 Prometheus 不修改
    runtime = c.app.state.runtime
    token = runtime.bind_request(operation=True)
    try:
        assert delete().status_code == 409
    finally:
        runtime.reset_request(token); runtime.end_operation()
    execution = c.app.state.container.execution_store().create(channel="agent", idempotency_key="running",
        host_id="node-a", action_name="restart", params={}, status="running")
    assert delete().status_code == 409
    c.app.state.container.execution_store().finish(execution.execution_id, status="succeeded")
    assert delete().status_code == 200
    assert c.app.state.container.execution_store().recent(host_id="node-a")[0].status == "succeeded"


def test_delete_rolls_back_both_files_and_runtime(tmp_path, monkeypatch):
    paths, settings, manager = _setup(tmp_path, target_mode="managed")
    monkeypatch.setattr(HostOnboarding, "_targets", lambda self: [])
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as c:
        c.post("/admin/hosts/add", headers=_auth("admin-token"), json={"revision": manager.revision(),
            "draft": {"host_id": "node-c", "name": "C", "exporter_address": "fixture:9100"}})
        before = {p: p.read_bytes() for p in (paths["hosts"], manager.target_path)}
        revision = manager.revision(); snapshot = manager.runtime.current
        write = manager._atomic_write
        failed = False
        def fail_once(path, text):
            nonlocal failed
            if path == manager.target_path and not failed:
                failed = True
                raise OSError("synthetic write failure")
            write(path, text)
        monkeypatch.setattr(manager, "_atomic_write", fail_once)
        response = c.request("DELETE", "/admin/hosts/node-c", headers=_auth("admin-token"),
                             json={"revision": revision, "stop_collection": True})
        assert response.status_code == 500
        assert manager.revision() == revision and manager.runtime.current is snapshot
        assert all(p.read_bytes() == content for p, content in before.items())
        manager.runtime.begin_apply(); manager.runtime.cancel_apply()


def test_history_filters_before_paging_and_separates_execution(access_app):
    c = access_app
    store = c.app.state.container.sessions(); history = c.app.state.container.approval_history()
    for i in range(30):
        host, user = ("node-a", "alice") if i < 28 else ("node-b", "bob")
        session = store.create(user=user, host_id=host)
        history.record_request({"approval_id": str(i), "session_id": session.session_id, "user": user,
            "host_id": host, "actions": [{"name": "restart", "args": {"service": "demo"}}], "created_at": "2026-10-13T00:00:00+00:00"})
        store.record_decision(str(i), session_id=session.session_id, action="approve", decided_by="admin", host_id=host)
    execution = c.app.state.container.execution_store().create(channel="agent", idempotency_key="failed",
        host_id="node-a", action_name="restart", params={}, approval_id="0", status="failed")
    data = c.get("/approvals/history?host_id=node-a&limit=10&offset=20", headers=_auth("admin-token")).json()
    assert data["total"] == 28 and len(data["items"]) == 8
    all_items = c.get("/approvals/history?host_id=node-a&limit=100", headers=_auth("admin-token")).json()["items"]
    item = next(x for x in all_items if x["approval_id"] == "0")
    assert item["status"] == "approve" and item["executions"][0]["status"] == "failed"
    assert c.get("/approvals/history", headers=_auth("user-token")).json()["items"] == []
    snap = c.get("/admin/config", headers=_auth("admin-token")).json()
    snap["config"]["settings"]["approval_mode"] = "owner"
    c.put("/admin/config", headers=_auth("admin-token"), json={"revision": snap["revision"], "config": snap["config"]})
    owner = c.get("/approvals/history?limit=100", headers=_auth("user-token")).json()
    assert owner["total"] == 28 and {x["requester"] for x in owner["items"]} == {"alice"}
    assert c.get("/approvals/history?host_id=node-b", headers=_auth("user-token")).status_code == 404
    # 删除会话不抹掉新冻结历史；授权撤销后不能读取旧记录。
    store.delete(item["session_id"], user="alice")
    assert c.get("/approvals/history?limit=100", headers=_auth("user-token")).json()["total"] == 28
    save_acl(c, {"alice": [], "bob": ["node-b"]})
    assert c.get("/approvals/history", headers=_auth("user-token")).json()["items"] == []


@pytest.mark.parametrize("status", ["expired", "cancelled", "evicted"])
def test_terminal_history_never_invents_human_decider(tmp_path, status):
    db = open_database(tmp_path / "history.db"); history = ApprovalHistory(db)
    history.record_request({"approval_id": "a", "host_id": "node-a", "user": "alice", "created_at": "2026-10-13T00:00:00+00:00"})
    history.terminal("a", status)
    item = history.page(identity="alice", hosts={"node-a"}, approver=False)["items"][0]
    assert item["status"] == status and item["decided_by"] == "" and item["decided_at"] == ""


def test_v7_migration_and_legacy_unknowns(tmp_path):
    from tests.test_sqlite_db import _structure
    path = tmp_path / "old.db"; open_database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE approval_requests"); conn.execute("PRAGMA user_version=7")
    db = open_database(path); fresh = tmp_path / "fresh.db"; open_database(fresh)
    assert _structure(path) == _structure(fresh)
    store = SqliteSessionStore(db)
    store.record_decision("legacy", session_id=None, action="reject", host_id="node-a")
    item = ApprovalHistory(db).page(identity="admin", hosts={"node-a"}, approver=True)["items"][0]
    assert item["requester"] == "" and item["created_at"] == "" and item["decided_by"] == ""
    assert ApprovalHistory(db).page(identity="alice", hosts={"node-a"}, approver=False)["items"] == []


def test_grafana_queries_share_catalog_and_keep_devices():
    from scripts.metric_contract import dashboard, expressions
    doc = dashboard()
    assert len(doc["panels"]) == 17
    assert [p["targets"][0]["expr"] for p in doc["panels"]] == list(expressions().values())
    assert 'device!="lo"' in expressions()["network.rx_bytes"]
    assert "[5m]" in expressions()["cpu.usage"]
    assert "mountpoint" in doc["panels"][6]["targets"][0]["legendFormat"]


@pytest.mark.parametrize("memory", [False, True])
def test_host_queue_reconciliation_does_not_abandon_other_hosts(tmp_path, memory):
    from src.storage.session import InMemorySessionStore
    store = InMemorySessionStore() if memory else SqliteSessionStore(open_database(tmp_path / "reconcile.db"))
    sessions = {}
    for host in ("node-a", "node-b"):
        session = store.create(user="alice", host_id=host)
        sessions[host] = session.session_id
        if not memory:
            store.record_request({"approval_id": host, "session_id": session.session_id, "host_id": host,
                "user": "alice", "created_at": "2026-10-13T00:00:00+00:00"})
        store.append_turn(session.session_id, user_content="request", assistant_content="pending",
                          status="pending_approval", approval_id=host)
    assert store.reconcile_pending([], host_id="node-a") == (1, 0)
    assert store.has_pending_approval(sessions["node-b"], "node-b")
    assert not store.has_pending_approval(sessions["node-a"], "node-a")
    if not memory:
        assert store.db.query("SELECT status FROM approval_requests WHERE approval_id = 'node-b'")[0]["status"] == "pending"


def test_ai_text_keeps_not_applicable_query_error_and_devices_separate():
    from types import SimpleNamespace
    from src.core.analysis import _trend_line
    from src.core.capabilities import metric_text
    from src.interfaces.monitor import MetricNotApplicable
    from src.metrics.catalog import METRIC_BY_ID
    def no_swap(*args, **kwargs):
        raise MetricNotApplicable("未启用 Swap")
    text = metric_text(SimpleNamespace(query_metric=no_swap), METRIC_BY_ID["memory.swap_usage"])
    assert "不适用" in text and "失败" not in text
    assert "不适用" in _trend_line("Swap", {"state": "not_applicable", "points": 0}, trend_minutes=60, unit="%")
    assert "查询失败" in _trend_line("磁盘", {"state": "query_error"}, trend_minutes=60, unit="%")
    line = _trend_line("网络", {"points": 4, "series": [
        {"labels": {"device": "eth0"}, "values": [[1, 0], [2, 0]]},
        {"labels": {"device": "eth1"}, "values": [[1, 2], [2, 3]]},
    ]}, trend_minutes=60, unit="MiB/s")
    assert "eth0" in line and "eth1" in line and "0.00 MiB/s" in line and "3.00 MiB/s" in line
