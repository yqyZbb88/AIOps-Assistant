"""状态日志归档存储：三表写入/读取、关联键、幂等、截断、清理（全离线）。"""
from __future__ import annotations

import sqlite3

import pytest

from src.interfaces.history import (
    MAX_ADVICE_CHARS,
    MAX_DETAIL_CHARS,
    AdviceRecord,
    AlertRecord,
    HistoryError,
    RemediationRecord,
)
from src.storage.db import open_database
from src.storage.history import SqliteHistoryStore
from src.storage.models import Alert


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def store(tmp_path):
    clock = FakeClock()
    # 彻底关掉自动清理：本文件要手动控制 purge 时机，否则写入时的顺带清理
    # 会在断言前就把旧行删掉，测试变成在测别的东西。
    # 必须用 inf 而不是某个大数——`_last_purge` 初值为 0，而 clock 是 epoch 秒（1.7e9），
    # 任何有限大数都会在**首次写入**时就被判为「已超间隔」而触发清理。
    return SqliteHistoryStore(
        open_database(tmp_path / "ops.db"),
        clock=clock, ttl_seconds=3600, max_rows=3,
        purge_interval_seconds=float("inf"),
    )


def _alert(key="k1", **kw) -> AlertRecord:
    return AlertRecord(alert_key=key, alertname=kw.pop("alertname", "HighCPU"),
                       severity="critical", instance="h:9100", summary="cpu busy",
                       status=kw.pop("status", "firing"), starts_at="2026-01-01T00:00:00+00:00",
                       ends_at="", received_at=kw.pop("received_at", "2026-01-01T00:00:01+00:00"))


def _remediation(key="k1", **kw) -> RemediationRecord:
    return RemediationRecord(alert_key=key, alertname="HighCPU", severity="critical",
                             instance="h:9100", matched_rule="cleanup_disk", action="cleanup_disk",
                             status=kw.pop("status", "ok"), detail=kw.pop("detail", "动作执行成功"),
                             executed_at=kw.pop("executed_at", "2026-01-01T00:00:02+00:00"))


def _advice(key="k1", **kw) -> AdviceRecord:
    return AdviceRecord(alert_key=key, alertname="HighCPU", severity="critical",
                        instance="h:9100", advice=kw.pop("advice", "建议先查进程"),
                        analyzed_at=kw.pop("analyzed_at", "2026-01-01T00:00:03+00:00"))


# -- 读写 -------------------------------------------------------------------
def test_record_batch_writes_all_three_tables(store):
    store.record_batch(alerts=[_alert()], remediations=[_remediation()], advice=[_advice()])

    assert [r.alertname for r in store.recent_alerts()] == ["HighCPU"]
    assert [r.matched_rule for r in store.recent_remediations()] == ["cleanup_disk"]
    assert [r.advice for r in store.recent_advice()] == ["建议先查进程"]


def test_empty_batch_is_noop(store):
    store.record_batch()
    assert store.count("alerts") == 0


def test_records_are_newest_first(store):
    store.record_batch(alerts=[_alert("k1", received_at="2026-01-01T00:00:01+00:00")])
    store.record_batch(alerts=[_alert("k2", received_at="2026-01-01T00:00:09+00:00")])

    assert [r.received_at for r in store.recent_alerts()] == [
        "2026-01-01T00:00:09+00:00", "2026-01-01T00:00:01+00:00"
    ]


def test_remediation_claims_are_purged_with_history(store):
    store.db.execute(
        "INSERT INTO remediation_claims "
        "(alert_key, action, alert_status, starts_at, claimed_at) VALUES (?,?,?,?,?)",
        ("old", "restart_service", "firing", "t", "2020-01-01T00:00:00+00:00"),
    )

    store.purge_expired()

    assert store.db.query("SELECT 1 FROM remediation_claims") == []


def _decide(store, approval_id, *, session_id="s1", decided_at, decided_by="alice"):
    store.db.execute(
        "INSERT INTO approval_decisions "
        "(approval_id, session_id, action, decided_at, decided_by) VALUES (?,?,?,?,?)",
        (approval_id, session_id, "approve", decided_at, decided_by),
    )


def test_approval_decisions_are_purged_with_history(store):
    """决策记录**不归本模块写**，但过期归它管（见 purge_expired 的说明）。

    关键是第二行：`session_id` 为空的行，会话侧那条「跟着会话删」的快路径永远扫不到
    （`WHERE session_id IN (...)` 匹配不上 NULL），只能靠按时间兜底。
    """
    _decide(store, "ap-old", decided_at="2020-01-01T00:00:00+00:00")
    _decide(store, "ap-null", session_id=None, decided_at="2020-01-01T00:00:00+00:00")

    store.purge_expired()

    assert store.db.query("SELECT 1 FROM approval_decisions") == []


def test_approval_decisions_capacity_is_capped(store):
    """保留期内的记录按 max_rows 上限保留最新的若干条（同归档表与幂等表）。"""
    for i in range(5):  # max_rows=3
        _decide(store, f"ap-{i}", decided_at=f"2026-01-01T00:00:0{i}+00:00")

    store.purge_expired()

    kept = store.db.query("SELECT approval_id FROM approval_decisions ORDER BY decided_at")
    assert [r["approval_id"] for r in kept] == ["ap-2", "ap-3", "ap-4"]


def test_limit_is_respected(store):
    for i in range(5):
        store.record_batch(alerts=[_alert(f"k{i}", received_at=f"2026-01-01T00:00:0{i}+00:00")])

    assert len(store.recent_alerts(limit=2)) == 2


def test_history_can_be_filtered_by_host(store):
    store.record_batch(
        alerts=[
            AlertRecord(
                **{**_alert("a").__dict__, "host_id": "host-a", "host_resolution": "matched"}
            ),
            AlertRecord(
                **{**_alert("b").__dict__, "host_id": "host-b", "host_resolution": "matched"}
            ),
        ]
    )

    rows = store.recent_alerts(host_id="host-a")
    assert [row.alert_key for row in rows] == ["a"]
    assert rows[0].host_resolution == "matched"


# -- 关联键 -----------------------------------------------------------------
def test_records_share_alert_key(store):
    store.record_batch(alerts=[_alert("abc")], remediations=[_remediation("abc")],
                       advice=[_advice("abc")])

    assert store.recent_alerts()[0].alert_key == "abc"
    assert store.recent_remediations()[0].alert_key == "abc"
    assert store.recent_advice()[0].alert_key == "abc"


def test_alert_key_falls_back_to_composite_when_fingerprint_empty():
    """fingerprint 默认空串（既有构造与测试都不设它）——空串当 join key 会把不同告警并成一条。"""
    a = Alert(labels={"alertname": "HighCPU", "instance": "h1"}, startsAt="t1")
    b = Alert(labels={"alertname": "HighCPU", "instance": "h2"}, startsAt="t1")

    assert a.fingerprint == ""
    assert a.alert_key != b.alert_key, "空 fingerprint 下落回复合键，两条告警不能并成一条"


def test_alert_key_prefers_fingerprint_when_present():
    a = Alert(fingerprint="fp-1", labels={"alertname": "HighCPU", "instance": "h1"})
    assert a.alert_key == "fp-1"


# -- 幂等与状态迁移 ----------------------------------------------------------
def test_same_alert_is_not_duplicated(store):
    """Alertmanager 会重投：同一条告警重复推送不应堆成多行。"""
    store.record_batch(alerts=[_alert()])
    store.record_batch(alerts=[_alert()])

    assert store.count("alerts") == 1


def test_firing_then_resolved_keeps_both(store):
    """firing → resolved 是同一告警的两个生命周期事件，都要留下。

    （若用 UNIQUE(alert_key) + REPLACE，「曾经 firing 过」的痕迹就没了。）
    """
    store.record_batch(alerts=[_alert(status="firing")])
    store.record_batch(alerts=[_alert(status="resolved")])

    assert {r.status for r in store.recent_alerts()} == {"firing", "resolved"}


# -- 安全：截断 --------------------------------------------------------------
def test_detail_is_truncated(store):
    """`detail` 里含远端命令 stdout，直接落盘会违反「整段输出不落日志」的不变量。"""
    store.record_batch(remediations=[_remediation(detail="X" * 5000)])

    stored = store.recent_remediations()[0].detail
    assert len(stored) < 5000
    assert stored.endswith("（已截断）")


def test_advice_is_truncated(store):
    store.record_batch(advice=[_advice(advice="Y" * 10000)])
    assert len(store.recent_advice()[0].advice) < 10000


def test_short_text_is_not_touched(store):
    store.record_batch(remediations=[_remediation(detail="短文本")])
    assert store.recent_remediations()[0].detail == "短文本"


def test_truncation_limits_are_sane():
    assert 0 < MAX_DETAIL_CHARS <= 2000, "detail 上限要小（可能含远端输出）"
    assert 0 < MAX_ADVICE_CHARS <= 10000, "advice 上限要给模型留够（但也不能无限）"


# -- 清理 -------------------------------------------------------------------
def test_purge_by_ttl(store):
    store.record_batch(alerts=[_alert(received_at="2020-01-01T00:00:00+00:00")])
    store.record_batch(alerts=[_alert("k2")])  # 这一条用默认的近期时间戳

    removed = store.purge_expired()

    assert removed >= 1
    assert [r.alert_key for r in store.recent_alerts()] == ["k2"]


def test_purge_by_capacity(store):
    for i in range(5):  # max_rows=3
        store.record_batch(alerts=[_alert(f"k{i}",
                                          received_at=f"2026-01-01T00:00:0{i}+00:00")])

    store.purge_expired()

    assert store.count("alerts") == 3
    assert [r.alert_key for r in store.recent_alerts()] == ["k4", "k3", "k2"]


def test_purge_covers_all_three_tables(store):
    with_old = dict(executed_at="2020-01-01T00:00:00+00:00")
    store.record_batch(
        alerts=[_alert(received_at="2020-01-01T00:00:00+00:00")],
        remediations=[_remediation(**with_old)],
        advice=[_advice(analyzed_at="2020-01-01T00:00:00+00:00")],
    )

    store.purge_expired()

    assert store.count("alerts") == 0
    assert store.count("remediations") == 0
    assert store.count("ai_advice") == 0


def test_count_rejects_unknown_table(store):
    with pytest.raises(ValueError):
        store.count("messages")


# -- 降级 -------------------------------------------------------------------
def test_write_failure_marks_unhealthy(tmp_path, monkeypatch):
    store = SqliteHistoryStore(open_database(tmp_path / "ops.db"))
    assert store.healthy is True

    def boom(*a, **k):
        raise sqlite3.OperationalError("模拟写失败")

    monkeypatch.setattr(store.db, "transaction", boom)

    with pytest.raises(HistoryError):
        store.record_batch(alerts=[_alert()])
    assert store.healthy is False
    assert "模拟写失败" in (store.last_error or "")


def test_health_recovers_after_a_successful_operation(tmp_path, monkeypatch):
    """与 SqliteSessionStore 同款：`healthy` 反映**最近一次**操作的结果。

    否则一次瞬时锁会让 `/health` 永远显示 degraded，而库其实好好的。
    """
    store = SqliteHistoryStore(open_database(tmp_path / "ops.db"))

    def boom(*a, **k):
        raise sqlite3.OperationalError("模拟一次瞬时失败")

    monkeypatch.setattr(store.db, "transaction", boom)
    with pytest.raises(HistoryError):
        store.record_batch(alerts=[_alert()])
    assert store.healthy is False

    monkeypatch.undo()  # 恢复

    store.record_batch(alerts=[_alert()])
    assert store.healthy is True, "成功一次就该恢复"
    assert store.last_error is None
