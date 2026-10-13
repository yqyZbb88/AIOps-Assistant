from src.storage.db import open_database
from src.storage.execution import SqliteActionExecutionStore


def test_execution_store_is_idempotent_and_host_filterable(tmp_path):
    db = open_database(tmp_path / "ops.db")
    store = SqliteActionExecutionStore(db)

    first = store.create(
        channel="rule",
        idempotency_key="rule:host-a:k",
        host_id="host-a",
        action_name="restart_service",
        params={"service": "nginx", "password": "secret"},
        status="running",
    )
    duplicate = store.create(
        channel="rule",
        idempotency_key="rule:host-a:k",
        host_id="host-a",
        action_name="restart_service",
        params={},
    )
    store.create(
        channel="agent",
        idempotency_key="agent:ap-1:0",
        host_id="host-b",
        action_name="cleanup_disk",
        params={},
        approval_id="ap-1",
        status="rejected",
    )
    store.finish(first.execution_id, status="succeeded", detail="ok")

    assert duplicate is None
    rows = store.recent(host_id="host-a")
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].params["password"] == "***"
    assert store.recent(host_id="host-b")[0].status == "rejected"
    assert store.recent(host_id="host-b")[0].finished_at


def test_rule_claim_and_execution_roll_back_together(tmp_path, monkeypatch):
    import pytest

    db = open_database(tmp_path / "ops.db")
    store = SqliteActionExecutionStore(db)
    original_create = store.create

    def fail_create(**kwargs):
        raise RuntimeError("audit insert failed")

    monkeypatch.setattr(store, "create", fail_create)
    kwargs = dict(
        alert_key="alert-1", action="restart_service",
        alert_status="firing", starts_at="2026-01-01T00:00:00Z",
        channel="rule", idempotency_key="rule:alert-1:restart_service",
        host_id="h1", action_name="restart_service", params={},
    )
    with pytest.raises(RuntimeError, match="audit insert"):
        store.claim_rule(**kwargs)
    assert db.query("SELECT * FROM remediation_claims") == []
    assert db.query("SELECT * FROM action_executions") == []

    monkeypatch.setattr(store, "create", original_create)
    created = store.claim_rule(**kwargs)
    assert created is not None
    assert created.alert_key == "alert-1"
    assert store.claim_rule(**kwargs) is None
    assert len(db.query("SELECT * FROM remediation_claims")) == 1
    assert len(db.query("SELECT * FROM action_executions")) == 1
