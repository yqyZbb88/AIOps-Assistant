"""真实 SQLite 共库抢占/审计的生产分支，不调用 SSH。"""
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.core.agent import OpsAgent
from src.core.rule_engine import DUPLICATE_MARK, RuleEngine
from src.storage.db import open_database
from src.storage.execution import SqliteActionExecutionStore
from src.storage.remediation_guard import SqliteRuleExecutionGuard
from src.utils.hosts_config import parse_hosts
from tests.conftest import FakeMonitor, make_alert, make_payload
from tests.test_multi_host_boundaries import HOSTS, TargetExecutor, _rules


def setup_engine(tmp_path):
    db = open_database(tmp_path / "audit.db")
    store = SqliteActionExecutionStore(db)
    executor = TargetExecutor()
    engine = RuleEngine(executors={"restart_service": executor}, rules_file=_rules(tmp_path),
                        hosts=parse_hosts(HOSTS), execution_store=store,
                        execution_guard=SqliteRuleExecutionGuard(db), require_execution_store=True)
    agent = OpsAgent(FakeMonitor(), rule_engine=engine, hosts=parse_hosts(HOSTS))
    payload = make_payload(make_alert("HighCPU", instance="a:9100"))
    payload.alerts[0].labels["job"] = "node-a"
    return db, store, executor, agent, payload


def test_sqlite_rule_claim_executes_once_and_keeps_alert_key(tmp_path):
    db, store, executor, agent, payload = setup_engine(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: agent.process_alert(payload).remediation[0], range(2)))
    assert sorted(r["status"] for r in results) == sorted(["ok", DUPLICATE_MARK])
    assert len(executor.calls) == 1
    execution = store.recent()[0]
    assert execution.status == "succeeded" and execution.alert_key == payload.alerts[0].alert_key
    assert execution.host_id == "host-a" and execution.execution_id
    assert len(db.query("SELECT * FROM remediation_claims")) == 1
    assert len(store.recent()) == 1


def test_rule_audit_failure_refuses_action_rolls_back_and_can_retry(tmp_path, monkeypatch):
    db, store, executor, agent, payload = setup_engine(tmp_path)
    create = store.create
    def fail(**kwargs):
        raise RuntimeError("synthetic audit failure")
    monkeypatch.setattr(store, "create", fail)
    result = agent.process_alert(payload).remediation[0]
    assert result["status"] == "failed" and executor.calls == []
    assert db.query("SELECT * FROM remediation_claims") == [] and store.recent() == []
    monkeypatch.setattr(store, "create", create)
    assert agent.process_alert(payload).remediation[0]["status"] == "ok"
    assert len(executor.calls) == 1
