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
