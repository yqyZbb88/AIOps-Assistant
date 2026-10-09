"""ChatService 多轮接线：历史注入、会话落库、降级（全离线，用内存 store）。"""
from __future__ import annotations

import pytest

from src.core.chat import DEFAULT_REPLY, ChatService
from src.interfaces.session import (
    ChatMessage,
    SessionError,
    SessionNotFoundError,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
    TurnResult,
)
from src.storage.session import InMemorySessionStore


class FakeHistoryAssistant:
    """实现 Assistant + HistoryCapable：记录收到的历史，按脚本返回结果。"""

    def __init__(self, replies: list[TurnResult] | None = None, *, error: Exception | None = None):
        self.seen: list[list[ChatMessage]] = []
        self._replies = list(replies or [])
        self._error = error

    def reply(self, message: str) -> str:
        return f"[单轮]{message}"

    def reply_turn(self, message: str, *, history=()) -> TurnResult:
        self.seen.append(list(history))
        if self._error is not None:
            raise self._error
        if self._replies:
            return self._replies.pop(0)
        return TurnResult(text=f"[多轮]{message}")


def _service(assistant, store=None, **kwargs) -> ChatService:
    from tests.conftest import FakeMonitor

    return ChatService(
        FakeMonitor(),
        assistant_factory=lambda: assistant,
        sessions=store,
        **kwargs,
    )


# -- 历史注入 ---------------------------------------------------------------
def test_history_is_passed_to_agent():
    store = InMemorySessionStore()
    info = store.create()
    store.append_turn(info.session_id, user_content="第一轮", assistant_content="回答一")
    agent = FakeHistoryAssistant()

    svc = _service(agent, store)
    svc.reply_turn("第二轮", session_id=info.session_id)

    assert [(m.role, m.content) for m in agent.seen[0]] == [
        ("user", "第一轮"),
        ("assistant", "回答一"),
    ]


def test_first_turn_has_empty_history():
    store = InMemorySessionStore()
    agent = FakeHistoryAssistant()

    svc = _service(agent, store)
    svc.reply_turn("你好")

    assert agent.seen == [[]]


def test_history_window_is_limited():
    store = InMemorySessionStore()
    info = store.create()
    for i in range(5):
        store.append_turn(info.session_id, user_content=f"q{i}", assistant_content=f"a{i}")
    agent = FakeHistoryAssistant()

    svc = _service(agent, store, message_window=4)
    svc.reply_turn("下一轮", session_id=info.session_id)

    assert [m.content for m in agent.seen[0]] == ["q3", "a3", "q4", "a4"]


def test_pending_turns_are_excluded_from_history():
    """待审批/作废的轮不进模型窗口（否则模型会以为还在等批准）。"""
    store = InMemorySessionStore()
    info = store.create()
    store.append_turn(info.session_id, user_content="重启 nginx", assistant_content="⚠️ 待审批",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")
    agent = FakeHistoryAssistant()

    svc = _service(agent, store)
    svc.reply_turn("在吗", session_id=info.session_id)

    assert agent.seen == [[]]


def test_falls_back_to_single_turn_for_plain_assistant():
    """只实现 reply 的旧对象（没有 reply_turn）仍可用：退回单轮，不传历史。"""
    class PlainAssistant:
        def reply(self, message: str) -> str:
            return f"[单轮]{message}"

    store = InMemorySessionStore()
    info = store.create()
    store.append_turn(info.session_id, user_content="旧", assistant_content="回答")

    svc = _service(PlainAssistant(), store)
    turn = svc.reply_turn("新", session_id=info.session_id)

    assert turn.reply == "[单轮]新"


# -- 会话落库 ---------------------------------------------------------------
def test_turn_is_recorded():
    store = InMemorySessionStore()
    svc = _service(FakeHistoryAssistant(), store)

    turn = svc.reply_turn("CPU 怎么样")

    assert turn.session_id
    msgs = store.messages(turn.session_id)
    assert [(m.role, m.content, m.status) for m in msgs] == [
        ("user", "CPU 怎么样", STATUS_FINAL),
        ("assistant", "[多轮]CPU 怎么样", STATUS_FINAL),
    ]


def test_session_id_is_returned_for_existing_session():
    store = InMemorySessionStore()
    info = store.create()
    svc = _service(FakeHistoryAssistant(), store)

    assert svc.reply_turn("继续", session_id=info.session_id).session_id == info.session_id


def test_unknown_session_id_raises_not_recreates():
    """未知 id 必须抛错——静默重建会让 DELETE 形同虚设。"""
    store = InMemorySessionStore()
    svc = _service(FakeHistoryAssistant(), store)

    with pytest.raises(SessionNotFoundError):
        svc.reply_turn("你好", session_id="ghost")


def test_pending_turn_is_recorded_as_pending():
    store = InMemorySessionStore()
    agent = FakeHistoryAssistant([
        TurnResult(text="⚠️ 待审批", status="pending_approval", approval_id="ap1")
    ])
    svc = _service(agent, store)

    turn = svc.reply_turn("重启 nginx")

    msgs = store.messages(turn.session_id)
    assert all(m.status == STATUS_PENDING_APPROVAL for m in msgs), "整轮都应是 pending"
    assert [m.approval_id for m in msgs] == ["ap1", "ap1"]


def test_keyword_fallback_is_also_recorded():
    """降级部署（无 Agent）里关键词回答同样入库，否则两套部署行为不一致。"""
    svc = _service(FakeHistoryAssistant(error=RuntimeError("模型挂了")))
    store = InMemorySessionStore()
    svc = _service(FakeHistoryAssistant(error=RuntimeError("模型挂了")), store)

    turn = svc.reply_turn("CPU")

    assert "CPU 使用率" in turn.reply
    assert [m.content for m in store.messages(turn.session_id)] == ["CPU", turn.reply]


# -- 降级：持久化不可用时聊天照常 ---------------------------------------------
def test_chat_works_without_store():
    svc = _service(FakeHistoryAssistant(), None)

    turn = svc.reply_turn("你好")

    assert turn.reply == "[多轮]你好"
    assert turn.session_id is None


def test_read_failure_degrades_to_empty_history():
    store = InMemorySessionStore()
    info = store.create()
    store.append_turn(info.session_id, user_content="旧", assistant_content="回答")

    def boom(*a, **k):
        raise SessionError("模拟读失败")

    store.history = boom  # type: ignore[method-assign]
    agent = FakeHistoryAssistant()
    svc = _service(agent, store)

    turn = svc.reply_turn("继续", session_id=info.session_id)

    assert agent.seen == [[]], "读失败按无历史处理"
    assert turn.reply == "[多轮]继续"


def test_write_failure_does_not_break_the_reply():
    """落库失败**只记日志**：聊天已经答完了，不能因为存不下就让用户拿不到回答。"""
    store = InMemorySessionStore()
    svc = _service(FakeHistoryAssistant(), store)
    store.append_turn = lambda *a, **k: (_ for _ in ()).throw(SessionError("模拟写失败"))  # type: ignore[method-assign]

    turn = svc.reply_turn("你好")

    assert turn.reply == "[多轮]你好"


def test_storage_healthy_reflects_store():
    """`/health` 的 degraded 判据：store 说它不健康，就得反映出来。"""

    class _UnhealthyStore(InMemorySessionStore):
        @property
        def healthy(self) -> bool:
            return False

    assert _service(FakeHistoryAssistant(), InMemorySessionStore()).storage_healthy is True
    assert _service(FakeHistoryAssistant(), _UnhealthyStore()).storage_healthy is False
    # 未启用存储不算故障：那时没有历史可丢，报 degraded 只会误导运维
    assert _service(FakeHistoryAssistant(), None).storage_healthy is True



@pytest.mark.parametrize("sqlite", [False, True])
def test_deleted_session_during_pending_turn_cancels_approval(tmp_path, sqlite):
    from src.storage.db import open_database
    from src.storage.session import SqliteSessionStore
    store = SqliteSessionStore(open_database(tmp_path / "sessions.db")) if sqlite else InMemorySessionStore()
    session = store.create()
    class LateApproval(FakeHistoryAssistant):
        cancelled = []
        def reply_turn(self, message, *, history=()):
            store.delete(session.session_id)
            return TurnResult(text="approval", status="pending_approval", approval_id="late-test-approval")
        def cancel_pending(self, approval_id):
            self.cancelled.append(approval_id)
            return True
    assistant = LateApproval()
    with pytest.raises(SessionError):
        _service(assistant, store).reply_turn("test", session_id=session.session_id)
    assert assistant.cancelled == ["late-test-approval"]
    assert store.messages(session.session_id) == []


def test_pending_history_failure_cancels_approval_but_normal_replies_still_degrade(monkeypatch):
    store = InMemorySessionStore()
    session = store.create()
    class PendingAssistant(FakeHistoryAssistant):
        cancelled = []
        def cancel_pending(self, approval_id):
            self.cancelled.append(approval_id)
            return True
    assistant = PendingAssistant([TurnResult(text="approval", status="pending_approval", approval_id="test-approval")])
    def fail(*args, **kwargs):
        raise SessionError("synthetic storage failure")
    monkeypatch.setattr(store, "append_turn", fail)
    with pytest.raises(SessionError):
        _service(assistant, store).reply_turn("test", session_id=session.session_id)
    assert assistant.cancelled == ["test-approval"]



def test_orphan_approval_is_not_resumed_when_cancellation_is_unavailable():
    from src.interfaces.approval import ApprovalError, PendingApproval
    store = InMemorySessionStore()
    session = store.create()
    store.delete(session.session_id)
    class OrphanAssistant(FakeHistoryAssistant):
        def pending_approvals(self):
            return [PendingApproval(approval_id="test-orphan", created_at="test-time", actions=(), session_id=session.session_id)]
        def decide(self, *args, **kwargs):
            raise AssertionError("deleted session must not resume")
    service = _service(OrphanAssistant(), store)
    service._ensure_assistant()
    with pytest.raises(ApprovalError, match="会话不存在"):
        service.decide("test-orphan", action="approve")


@pytest.mark.parametrize("failure", ["storage_error", "missing_turn"])
def test_unsaved_chained_approval_is_cancelled(failure, monkeypatch):
    from src.interfaces.approval import ApprovalOutcome, ApprovalOutcomeUnknownError, PendingApproval
    store = InMemorySessionStore()
    session = store.create()
    store.append_turn(session.session_id, user_content="test-first", assistant_content="test-pending",
                      status=STATUS_PENDING_APPROVAL, approval_id="test-first")
    class ChainedAssistant(FakeHistoryAssistant):
        cancelled = []
        def pending_approvals(self):
            return [PendingApproval(approval_id="test-first", created_at="test-time", actions=(), session_id=session.session_id)]
        def decide(self, approval_id, *, action, reason=""):
            return ApprovalOutcome(approval_id, action, "pending_approval", "test-next", next_approval_id="test-next")
        def cancel_pending(self, approval_id):
            self.cancelled.append(approval_id)
            return True
    assistant = ChainedAssistant()
    service = _service(assistant, store)
    service._ensure_assistant()
    if failure == "storage_error":
        def fail(*args, **kwargs):
            raise SessionError("synthetic storage failure")
        monkeypatch.setattr(store, "update_pending", fail)
    else:
        monkeypatch.setattr(store, "update_pending", lambda *args, **kwargs: False)
    with pytest.raises(ApprovalOutcomeUnknownError, match="consumed"):
        service.decide("test-first", action="approve")
    assert assistant.cancelled == ["test-next"]


@pytest.mark.parametrize("sqlite", [False, True])
@pytest.mark.parametrize("published", [False, True])
def test_approval_requires_saved_turn_even_outside_message_window(tmp_path, sqlite, published):
    from src.interfaces.approval import ApprovalOutcome, ApprovalUnavailableError, PendingApproval
    from src.storage.db import open_database
    from src.storage.session import SqliteSessionStore
    store = SqliteSessionStore(open_database(tmp_path / "sessions.db")) if sqlite else InMemorySessionStore()
    session = store.create()
    if published:
        store.append_turn(session.session_id, user_content="test-write", assistant_content="test-pending",
                          status=STATUS_PENDING_APPROVAL, approval_id="test-unpublished")
        for i in range(60):
            store.append_turn(session.session_id, user_content=f"test-q-{i}", assistant_content="test-final")
    class PublishingAssistant(FakeHistoryAssistant):
        calls = []
        def pending_approvals(self):
            return [PendingApproval(approval_id="test-unpublished", created_at="test-time", actions=(), session_id=session.session_id)]
        def decide(self, approval_id, *, action, reason=""):
            self.calls.append(approval_id)
            return ApprovalOutcome(approval_id, action, "completed", "test-complete")
    assistant = PublishingAssistant()
    service = _service(assistant, store)
    service._ensure_assistant()
    if published:
        service.decide("test-unpublished", action="approve")
        assert assistant.calls == ["test-unpublished"]
    else:
        with pytest.raises(ApprovalUnavailableError, match="保存"):
            service.decide("test-unpublished", action="approve")
        assert assistant.calls == []



def test_failed_cancellation_cannot_make_unsaved_chain_executable(monkeypatch):
    from src.interfaces.approval import ApprovalOutcome, ApprovalOutcomeUnknownError, ApprovalUnavailableError, PendingApproval
    store = InMemorySessionStore()
    session = store.create()
    store.append_turn(session.session_id, user_content="test-first", assistant_content="test-pending",
                      status=STATUS_PENDING_APPROVAL, approval_id="test-first")
    class UncancellableChain(FakeHistoryAssistant):
        pending_id = "test-first"
        calls = []
        def pending_approvals(self):
            return [PendingApproval(self.pending_id, "test-time", session_id=session.session_id)]
        def decide(self, approval_id, *, action, reason=""):
            self.calls.append(approval_id)
            self.pending_id = "test-next"
            return ApprovalOutcome(approval_id, action, "pending_approval", "test-next", next_approval_id="test-next")
        def cancel_pending(self, approval_id):
            return False
    assistant = UncancellableChain()
    service = _service(assistant, store)
    service._ensure_assistant()
    monkeypatch.setattr(store, "update_pending", lambda *args, **kwargs: False)
    with pytest.raises(ApprovalOutcomeUnknownError):
        service.decide("test-first", action="approve")
    with pytest.raises(ApprovalUnavailableError, match="保存"):
        service.decide("test-next", action="approve")
    assert assistant.calls == ["test-first"]
