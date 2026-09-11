"""多轮 × 人工审批：中断落 pending、审批后回填、作废、删除会话（真 graph，全离线）。

这里验证的是**跨模块**的行为：ChatService（core）↔ OpsAssistant（真 graph）↔
SessionStore（SQLite）。每一段单独看都对、接起来错，正是这类用例存在的理由。
"""
from __future__ import annotations

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.messages import AIMessage  # noqa: E402

from src.agent.assistant import OpsAssistant  # noqa: E402
from src.core.chat import ChatService  # noqa: E402
from src.interfaces.approval import ApprovalError  # noqa: E402
from src.interfaces.session import (  # noqa: E402
    STATUS_ABANDONED,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
)
from src.storage.approval import ApprovalStore  # noqa: E402
from src.storage.db import open_database  # noqa: E402
from src.storage.session import SqliteSessionStore  # noqa: E402
from tests.conftest import FakeAgentActions, FakeMonitor  # noqa: E402
from tests.test_agent_actions_hitl_graph import ScriptedChatModel, _tool_call  # noqa: E402


@pytest.fixture
def store(tmp_path) -> SqliteSessionStore:
    return SqliteSessionStore(open_database(tmp_path / "ops.db"), purge_interval_seconds=0)


def _service(monkeypatch, responses, store) -> ChatService:
    """ChatService + 真 graph 的 OpsAssistant（模型被替换为脚本化假模型）。"""
    from src.agent import assistant as assistant_mod

    monkeypatch.setattr(
        assistant_mod, "get_model", lambda: ScriptedChatModel(responses=responses)
    )
    asst = OpsAssistant(
        monitor=FakeMonitor(),
        diag=None,
        agent_actions=FakeAgentActions(),
        approvals=ApprovalStore(),
    )
    return ChatService(
        FakeMonitor(), assistant_factory=lambda: asst, sessions=store
    )


# -- 中断 → 落 pending -------------------------------------------------------
def test_interrupt_is_recorded_as_pending(monkeypatch, store):
    svc = _service(monkeypatch, [_tool_call("restart_service", {"service": "nginx"})], store)

    turn = svc.reply_turn("帮我重启 nginx")

    assert turn.approval_id
    msgs = store.messages(turn.session_id)
    assert len(msgs) == 2, "本轮 user + assistant 两行都要落"
    assert {m.status for m in msgs} == {STATUS_PENDING_APPROVAL}
    assert {m.approval_id for m in msgs} == {turn.approval_id}
    assert store.get(turn.session_id).pending_approvals == 1
    assert store.history(turn.session_id) == [], "pending 轮不进模型窗口"


def test_pending_turn_is_not_in_next_turn_history(monkeypatch, store):
    """中断那轮若只标 assistant 行，下一轮窗口里会出现「没回答的提问」——这里锁死整轮。"""
    svc = _service(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"}), AIMessage(content="在的")],
        store,
    )
    first = svc.reply_turn("帮我重启 nginx")

    svc.reply_turn("在吗", session_id=first.session_id)

    contents = [m.content for m in store.history(first.session_id)]
    assert contents == ["在吗", "在的"], "中断那轮整轮都不该进窗口"


# -- 审批 → 回填 -------------------------------------------------------------
def test_decide_backfills_final_answer(monkeypatch, store):
    """中断那轮的回答是在 decide() 时才产生的——不回填历史里就永远没有下文。"""
    svc = _service(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"}), AIMessage(content="已重启 nginx")],
        store,
    )
    turn = svc.reply_turn("帮我重启 nginx")

    outcome = svc.decide(turn.approval_id, action="approve")

    assert outcome.status == "completed"
    msgs = store.messages(turn.session_id)
    assert len(msgs) == 2, "回填是原地 UPDATE，不追加新行"
    assert [m.content for m in msgs] == ["帮我重启 nginx", "已重启 nginx"]
    assert {m.status for m in msgs} == {STATUS_FINAL}
    assert [m.content for m in store.history(turn.session_id)] == ["帮我重启 nginx", "已重启 nginx"]
    assert store.get(turn.session_id).pending_approvals == 0


def test_decide_reject_backfills_too(monkeypatch, store):
    svc = _service(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"}), AIMessage(content="好的，已取消")],
        store,
    )
    turn = svc.reply_turn("帮我重启 nginx")

    svc.decide(turn.approval_id, action="reject")

    assert store.messages(turn.session_id)[1].status == STATUS_FINAL


def test_chained_approval_swaps_approval_id(monkeypatch, store):
    """续跑又触发下一条写命令：同一行推进到下一个 id，而不是新开一行。"""
    svc = _service(
        monkeypatch,
        [
            _tool_call("restart_service", {"service": "nginx"}, call_id="c1"),
            # 必须用**目录里声明过**的命令：未声明的工具名不会被中间件拦，
            # 会直接穿过审批（这里曾用 cleanup_disk，结果 decide 返回 completed）
            _tool_call("restart_service", {"service": "mysql"}, call_id="c2"),
            # 脚本化模型用尽后会**重复末条**，故必须给一条终结回复，否则无限调工具
            AIMessage(content="都办好了"),
        ],
        store,
    )
    turn = svc.reply_turn("重启 nginx")
    first_seq = [m.seq for m in store.messages(turn.session_id)]

    outcome = svc.decide(turn.approval_id, action="approve")

    assert outcome.status == "pending_approval"
    assert outcome.next_approval_id
    msgs = store.messages(turn.session_id)
    assert len(msgs) == 2, "链式推进不追加行"
    assert [m.seq for m in msgs] == first_seq
    assert {m.approval_id for m in msgs} == {outcome.next_approval_id}
    assert {m.status for m in msgs} == {STATUS_PENDING_APPROVAL}


def test_decide_unknown_id_marks_abandoned(monkeypatch, store):
    """审批已不在队列里（被 TTL/容量淘汰）→ 历史那轮不可能再有下文，标作废。"""
    svc = _service(monkeypatch, [_tool_call("restart_service", {"service": "nginx"})], store)
    turn = svc.reply_turn("帮我重启 nginx")
    svc._assistant.approvals._records.clear()  # 模拟被淘汰

    with pytest.raises(ApprovalError):
        svc.decide(turn.approval_id, action="approve")

    assert store.messages(turn.session_id)[1].status == STATUS_ABANDONED
    assert store.history(turn.session_id) == []


# -- 删除会话 → 未决审批一并作废 ----------------------------------------------
def test_cancel_pending_removes_from_queue_and_abandons_row(monkeypatch, store):
    svc = _service(monkeypatch, [_tool_call("restart_service", {"service": "nginx"})], store)
    turn = svc.reply_turn("帮我重启 nginx")

    assert svc.cancel_pending(turn.approval_id) is True
    assert svc.pending_approvals() == [], "作废后不得再可批准"

    # 命中时会把行标成 abandoned（由 api 层在删会话前调用；这里直接验证审批侧效果）
    assert store.update_pending(turn.approval_id, content="", status=STATUS_ABANDONED) is True


def test_cancel_unknown_returns_false(monkeypatch, store):
    svc = _service(monkeypatch, [AIMessage(content="x")], store)
    svc.reply_turn("你好")
    assert svc.cancel_pending("nope") is False


def test_cancel_without_assistant_returns_false(store):
    """Agent 未构建时作废是 no-op，不触发构建、不抛错。"""
    svc = ChatService(FakeMonitor(), sessions=store)
    assert svc.cancel_pending("nope") is False
