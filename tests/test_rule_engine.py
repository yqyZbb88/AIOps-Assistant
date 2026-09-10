"""rule_engine：假执行器覆盖 ok / failed / skip / unmatched / 同 alertname 多条顺序。"""
from __future__ import annotations

from src.core.rule_engine import RuleEngine, SKIP_MARK
from src.interfaces.action import ActionError, ActionExecutor
from src.storage.models import AlertmanagerPayload

from tests.conftest import make_alert, make_payload

_RULES_YAML = """\
rules:
  - alertname: HighCPUUsage
    action: ok_action
    params:
      host: h1
      password: "${FAKE_SSH_PASSWORD:-placeholder}"
    auto_execute: true
    description: ok rule
  - alertname: HighCPUUsage
    action: fail_action
    params:
      host: h1
      password: "x"
    auto_execute: true
    description: fail rule
  - alertname: HighCPUUsage
    action: ok_action
    params:
      host: h1
    auto_execute: false
    description: skip rule (same alertname, in file order last)
  - alertname: HighMemoryUsage
    action: ok_action
    params:
      host: h2
    auto_execute: false
    description: skip rule for memory
"""


class OkExecutor(ActionExecutor):
    name = "ok_action"
    description = "假成功执行器"
    executed: list[dict] = []

    def __init__(self) -> None:
        self.executions: list[dict] = []

    def execute(self, params: dict) -> dict:
        self.executions.append(dict(params))
        OkExecutor.executed.append(dict(params))
        return {"ok": True, "host": params.get("host")}


class FailExecutor(ActionExecutor):
    name = "fail_action"
    description = "假失败执行器"

    def execute(self, params: dict) -> dict:
        raise ActionError(f"模拟远端失败 host={params.get('host')}")


def _engine(tmp_path) -> RuleEngine:
    rules_file = tmp_path / "rules.yaml"
    rules_file.write_text(_RULES_YAML, encoding="utf-8")
    return RuleEngine(executors={"ok_action": OkExecutor(), "fail_action": FailExecutor()},
                      rules_file=rules_file)


def test_rule_engine_loads_and_interpolates_params(tmp_path):
    engine = _engine(tmp_path)
    # 规则结构照搬；加载即对密码做 ${VAR:-default} 插值
    rules = engine.rules
    assert len(rules) == 4
    # 有 env 时取 env，无 env 时取 default；此处 FAKE_SSH_PASSWORD 未设置
    assert rules[0].params["password"] == "placeholder"


def test_rule_engine_ok_failed_skip_in_file_order(tmp_path):
    engine = _engine(tmp_path)
    ok = engine.executors["ok_action"]
    records = engine.execute_for_payload(make_payload(make_alert("HighCPUUsage")))

    # HighCPUUsage 配了 3 条：ok(true) → fail(true) → ok(false, skip)
    assert [r["status"] for r in records] == ["ok", "failed", SKIP_MARK]
    assert records[0]["params"]["password"] == "***"  # 记录打码，不落明文
    assert "password" not in records[0]["detail"]
    assert records[1]["detail"].startswith("动作执行失败:")
    assert ok.executions  # 成功执行器的确被调用了


def test_rule_engine_resolved_skipped(tmp_path):
    engine = _engine(tmp_path)
    payload = make_payload(make_alert("HighCPUUsage", status="resolved"))
    records = engine.execute_for_payload(payload)
    assert records == []  # 已解决告警不参与处置


def test_rule_engine_unmatched_alerts(tmp_path):
    engine = _engine(tmp_path)
    payload = make_payload(
        make_alert("HighCPUUsage"),      # 规则表内
        make_alert("UnknownAlert"),      # 规则表外
        make_alert("AnotherUnknown", status="resolved"),
    )
    # 处置只命中表内告警
    assert len(engine.execute_for_payload(payload)) > 0
    # 无规则且未解决的告警才进 AI 分析候选
    unmatched = engine.unmatched_alerts(payload)
    assert [a.alertname for a in unmatched] == ["UnknownAlert"]
    assert len(engine.execute_for_payload(
        make_payload(make_alert("UnknownAlert"))
    )) == 0


def test_rule_engine_reload_and_missing_file(tmp_path):
    engine = _engine(tmp_path)
    assert len(engine.rules) == 4
    # 缺失文件：加载为空表不抛错
    empty = RuleEngine(executors={"ok_action": OkExecutor()},
                       rules_file=tmp_path / "absent.yaml")
    assert empty.rules == []
