"""AI 数据时效说明与异常巡检查询工具。"""
from __future__ import annotations

from src.agent.prompts import OPS_SYSTEM_PROMPT
from src.agent.tools import build_inspection_tools, build_tools
from src.core.analysis import build_alert_context
from src.interfaces.diagnostic import render_diag
from src.interfaces.inspection import (
    LEVEL_CRITICAL,
    InspectionFinding,
    InspectionResult,
    InspectionStore,
)
from tests.conftest import FakeLLM, FakeMonitor, make_alert


class FakeInspectionStore(InspectionStore):
    def __init__(self, records):
        self.records = list(records)

    def record(self, result):
        self.records.append(result)

    def recent(self, *, limit=50):
        return self.records[:limit]

    def purge_expired(self):
        return 0


def _record():
    return InspectionResult(
        record_id=7,
        checked_at="2026-09-15T22:15:01+00:00",
        level=LEVEL_CRITICAL,
        snapshot={"cpu.usage": {"value": 95.0}},
        findings=(InspectionFinding("cpu.usage", LEVEL_CRITICAL, "CPU 使用率 95.0% 达到 critical"),),
    )


def test_inspection_tool_marks_records_as_historical():
    tool = build_inspection_tools(FakeInspectionStore([_record()]))[0]

    text = tool.invoke({})

    assert "id=7" in text
    assert "CPU 使用率 95.0%" in text
    assert "[数据时效]" in text
    assert "历史归档" in text
    assert "不代表当前状态" in text


def test_ops_assistant_registers_inspection_tool(monkeypatch):
    from langchain_core.messages import AIMessage

    from src.agent import assistant as assistant_mod
    from src.agent.assistant import OpsAssistant
    from tests.test_agent_actions_hitl_graph import ScriptedChatModel

    monkeypatch.setattr(
        assistant_mod,
        "get_model",
        lambda: ScriptedChatModel(responses=[AIMessage(content="ok")]),
    )
    assistant = OpsAssistant(
        FakeMonitor(),
        inspection_store=FakeInspectionStore([_record()]),
    )

    assert "inspection_anomalies" in {tool.name for tool in assistant.tools}


def test_inspection_tool_is_not_registered_without_store():
    assert build_inspection_tools(None) == []


def test_metric_tool_includes_freshness_note():
    tool = next(t for t in build_tools(FakeMonitor()) if t.name == "cpu_usage")

    text = tool.invoke({})

    assert "[数据时效]" in text
    assert "查询时刻快照" in text


def test_diagnostic_render_includes_freshness_note():
    text = render_diag(
        {"host": "h", "command": "memory", "exit_code": 0, "output": "ok", "executed_at": "2026-09-15T22:00:00+00:00"}
    )
    assert "[数据时效]" in text
    assert "2026-09-15T22:00:00+00:00" in text


def test_alert_analysis_context_includes_freshness_note():
    context = build_alert_context(make_alert("A"), FakeMonitor(), metrics_config=None)
    assert "[数据时效]" in context
    assert "不能假设仍持续 firing" in context


def test_system_prompt_defines_temporal_rules():
    assert "所有告警、指标、诊断、写命令和巡检归档结果都具有时效性" in OPS_SYSTEM_PROMPT
    assert "不得假设告警仍然 firing" in OPS_SYSTEM_PROMPT
