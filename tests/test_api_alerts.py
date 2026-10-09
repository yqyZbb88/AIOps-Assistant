"""状态日志归档的端到端：POST /alert 落盘 → GET /alerts/history 可查（全离线）。

LLM 显式关掉（`llm.enabled=false`）：一是保持离线，二是让「无规则告警」不产生
AI 建议——本文件要验的是归档链路，不是分析质量。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.utils.config_loader import AuthSettings, LLMSettings, Settings, StorageSettings


def _settings(tmp_path, *, enabled: bool = True) -> Settings:
    return Settings(
        auth=AuthSettings(tokens='{"test-ops":"ops"}', admin_identities='["ops"]'),
        llm=LLMSettings(enabled=False),
        storage=StorageSettings(enabled=enabled, db_path=str(tmp_path / "ops.db")),
    )


def _payload(*alerts: dict) -> dict:
    return {"status": "firing", "receiver": "ops", "alerts": list(alerts)}


def _alert(alertname: str = "HighCPUUsage", instance: str = "h1:9100", **kw) -> dict:
    return {
        "status": kw.pop("status", "firing"),
        "labels": {"alertname": alertname, "severity": "critical", "instance": instance},
        "annotations": {"summary": kw.pop("summary", "cpu busy")},
        "startsAt": kw.pop("startsAt", "2026-01-01T00:00:00+00:00"),
        "endsAt": "",
    }


@pytest.fixture
def client(tmp_path, monkeypatch, enabled_catalogs) -> TestClient:
    rules = tmp_path / "rules.yaml"
    rules.write_text("auto_execute_enabled: false\nrules:\n  - alertname: HighCPUUsage\n    action: restart_service\n    params: {service: nginx}\n    auto_execute: false\n", encoding="utf-8")
    monkeypatch.setenv("OPS_RULES_FILE", str(rules))
    from src.main import create_app

    with TestClient(create_app(settings=_settings(tmp_path)), headers={"Authorization": "Bearer test-ops"}) as c:
        c.store = c.app.state.container.history()  # type: ignore[attr-defined]
    return c


@pytest.fixture
def disabled_client(tmp_path) -> TestClient:
    from src.main import create_app

    with TestClient(create_app(settings=_settings(tmp_path, enabled=False)), headers={"Authorization": "Bearer test-ops"}) as c:
        c.path = tmp_path / "ops.db"  # type: ignore[attr-defined]
    return c


# -- 归档链路 ---------------------------------------------------------------
def test_alert_is_archived(client):
    """告警本身落盘——以前只在内存 deque 里，重启即丢。"""
    client.post("/alert", json=_payload(_alert()))

    body = client.get("/alerts/history").json()

    assert body["storage"] == "ok"
    assert [(a["alertname"], a["status"]) for a in body["alerts"]] == [("HighCPUUsage", "firing")]
    assert body["alerts"][0]["instance"] == "h1:9100"


def test_remediation_is_archived(client):
    """HighCPUUsage 在显式测试规则中命中 restart_service，但默认不自动执行。"""
    client.post("/alert", json=_payload(_alert("HighCPUUsage")))

    body = client.get("/alerts/history").json()

    assert len(body["remediations"]) == 1
    assert {r["status"] for r in body["remediations"]} == {"matched_but_auto_execute_disabled"}
    assert {r["matched_rule"] for r in body["remediations"]} == {"restart_service"}


def test_remediation_links_to_alert(client):
    """三张表靠 alert_key 关联（不是靠 alertname 字符串猜）。"""
    client.post("/alert", json=_payload(_alert()))

    body = client.get("/alerts/history").json()

    assert body["remediations"], "该告警应命中规则"
    assert {r["alert_key"] for r in body["remediations"]} == {body["alerts"][0]["alert_key"]}


def test_unmatched_alert_is_archived_without_remediation(client):
    client.post("/alert", json=_payload(_alert("SomethingElse")))

    body = client.get("/alerts/history").json()

    assert [a["alertname"] for a in body["alerts"]] == ["SomethingElse"]
    assert body["remediations"] == [], "无规则命中就不该有处置记录"
    assert body["advice"] == [], "LLM 已关闭 → 不产生 AI 建议"


def test_resolved_alert_is_archived(client):
    client.post("/alert", json=_payload(_alert(status="resolved")))

    body = client.get("/alerts/history").json()

    assert [a["status"] for a in body["alerts"]] == ["resolved"]
    assert body["remediations"] == [], "resolved 不参与处置"


def test_history_limit(client):
    for i in range(5):
        client.post("/alert", json=_payload(_alert(instance=f"h{i}:9100")))

    assert len(client.get("/alerts/history", params={"limit": 2}).json()["alerts"]) == 2


def test_history_survives_restart(tmp_path):
    """换一个 app 实例仍能查到——这才是「归档」与「内存缓冲」的区别。"""
    from src.main import create_app

    with TestClient(create_app(settings=_settings(tmp_path)), headers={"Authorization": "Bearer test-ops"}) as c1:
        c1.post("/alert", json=_payload(_alert()))

    with TestClient(create_app(settings=_settings(tmp_path)), headers={"Authorization": "Bearer test-ops"}) as c2:
        body = c2.get("/alerts/history").json()

    assert [a["alertname"] for a in body["alerts"]] == ["HighCPUUsage"]


# -- 降级 -------------------------------------------------------------------
def test_history_is_empty_when_storage_disabled(disabled_client):
    """能力缺失 = 空列表而非报错（沿用 /approvals、/sessions 的约定）。"""
    resp = disabled_client.get("/alerts/history")

    assert resp.status_code == 200
    assert resp.json() == {"alerts": [], "remediations": [], "advice": [], "storage": "off"}


def test_alert_still_works_when_storage_disabled(disabled_client):
    """归档关掉时告警接收照常（这是本服务的主体功能，不能被配存储拖垮）。"""
    resp = disabled_client.post("/alert", json=_payload(_alert()))

    assert resp.status_code == 200
    assert resp.json()["received"] == 1
    assert not disabled_client.path.exists(), "关闭时不该建库文件"


# -- 直接装配 OpsAgent 的两条（需要注入 FakeLLM / 假 store） -------------------
def _agent(tmp_path, *, history=None, llm=None):
    from src.core.agent import OpsAgent
    from src.core.analysis import AlertAnalyzer
    from src.core.rule_engine import RuleEngine

    monitor = _FakeMonitor()
    return OpsAgent(
        monitor=monitor,
        rule_engine=RuleEngine(executors={}),  # 无任何规则 → 全部走 AI 分析
        analysis=AlertAnalyzer(monitor, llm=llm),
        history=history,
    )


def _FakeMonitor():
    from tests.conftest import FakeMonitor

    return FakeMonitor()


def test_advice_is_archived(tmp_path):
    """AI 建议以前只随响应返回一次就蒸发，现在要落盘。"""
    from src.storage.db import open_database
    from src.storage.history import SqliteHistoryStore
    from src.storage.models import AlertmanagerPayload
    from tests.conftest import FakeLLM

    store = SqliteHistoryStore(open_database(tmp_path / "ops.db"))
    agent = _agent(tmp_path, history=store, llm=FakeLLM(text="建议先查进程"))

    agent.process_alert(AlertmanagerPayload(**_payload(_alert("SomethingElse"))))

    advice = store.recent_advice()
    assert [a.advice for a in advice] == ["建议先查进程"]
    assert advice[0].alert_key == store.recent_alerts()[0].alert_key, "靠 alert_key 串起来"


def test_archive_failure_is_retryable(tmp_path, monkeypatch):
    """归档失败必须显式报错，告警发送方才能重试。"""
    from src.storage.db import open_database
    from src.storage.history import SqliteHistoryStore
    from src.storage.models import AlertmanagerPayload
    from tests.conftest import FakeLLM

    store = SqliteHistoryStore(open_database(tmp_path / "ops.db"))
    monkeypatch.setattr(
        store, "record_batch",
        lambda **k: (_ for _ in ()).throw(RuntimeError("模拟归档炸了")),
    )
    agent = _agent(tmp_path, history=store, llm=FakeLLM())

    from src.interfaces.history import HistoryError

    with pytest.raises(HistoryError):
        agent.process_alert(AlertmanagerPayload(**_payload(_alert())))

    assert agent.recent(10), "内存记录仍可查看"


def test_no_history_store_is_fine(tmp_path):
    """没注入归档存储时 process_alert 完全照常（能力缺失即降级）。"""
    from src.storage.models import AlertmanagerPayload

    agent = _agent(tmp_path, history=None, llm=None)

    ack = agent.process_alert(AlertmanagerPayload(**_payload(_alert())))

    assert ack.received == 1


def test_recent_endpoint_unchanged(client):
    """/alerts/recent 仍是内存热缓冲语义，与 /alerts/history 不是一个东西。"""
    client.post("/alert", json=_payload(_alert()))

    recent = client.get("/alerts/recent").json()

    assert [r["alertname"] for r in recent] == ["HighCPUUsage"]
    assert "alert_key" not in recent[0], "内存摘要不含归档关联键（两者数据源不同）"


def test_archive_failure_returns_retryable_http_error(client, monkeypatch):
    history = client.app.state.container.history()
    monkeypatch.setattr(
        history, "record_batch",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("disk unavailable")),
    )
    response = client.post("/alert", json=_payload(_alert("SomethingElse")))
    assert response.status_code == 503
    assert "archive failed" in response.json()["detail"]
