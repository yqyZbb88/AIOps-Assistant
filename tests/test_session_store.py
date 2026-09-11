"""会话存储：SQLite 与内存两套实现跑同一组契约测试（离线）。"""
from __future__ import annotations

import pytest

from src.interfaces.session import (
    STATUS_ABANDONED,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
    SessionError,
)
from src.storage.db import open_database
from src.storage.session import InMemorySessionStore, SqliteSessionStore


class FakeClock:
    """可控时钟：离线验证 TTL 过期，不依赖真实等待（沿用 test_approval_store 的范式）。"""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(params=["sqlite", "memory"])
def store(request, tmp_path):
    """两套实现共享同一组断言——契约漂移会立刻暴露。"""
    clock = FakeClock()
    if request.param == "sqlite":
        db = open_database(tmp_path / "ops.db")
        return SqliteSessionStore(db, clock=clock, ttl_seconds=3600, max_sessions=3,
                                  purge_interval_seconds=0)
    return InMemorySessionStore(clock=clock, ttl_seconds=3600, max_sessions=3)


# -- 生命周期 ---------------------------------------------------------------
def test_create_and_get(store):
    info = store.create(user="ops")

    assert info.session_id
    assert store.get(info.session_id).user == "ops"
    assert info.message_count == 0


def test_get_unknown_returns_none(store):
    assert store.get("nope") is None


def test_list_is_newest_first(store):
    first = store.create(user="a")
    second = store.create(user="b")

    assert [s.session_id for s in store.list()] == [second.session_id, first.session_id]


def test_delete_removes_session_and_messages(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="hi", assistant_content="hello")

    assert store.delete(info.session_id) is True
    assert store.get(info.session_id) is None
    assert store.messages(info.session_id) == []


def test_delete_unknown_returns_false(store):
    assert store.delete("nope") is False


# -- 消息 -------------------------------------------------------------------
def test_append_turn_writes_two_rows_with_increasing_seq(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="CPU 怎么样", assistant_content="45%")

    msgs = store.messages(info.session_id)

    assert [(m.role, m.content, m.seq) for m in msgs] == [
        ("user", "CPU 怎么样", 1),
        ("assistant", "45%", 2),
    ]


def test_append_turn_sets_title_from_first_user_message(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="帮我看看磁盘", assistant_content="ok")
    store.append_turn(info.session_id, user_content="再看看内存", assistant_content="ok")

    assert store.get(info.session_id).title == "帮我看看磁盘"


def test_seq_keeps_increasing_across_turns(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="a", assistant_content="b")
    store.append_turn(info.session_id, user_content="c", assistant_content="d")

    assert [m.seq for m in store.messages(info.session_id)] == [1, 2, 3, 4]


def test_history_excludes_pending_and_abandoned(store):
    """窗口只给模型看 final——pending/abandoned 是控制面信息，会诱发「还在等批准」的幻觉。"""
    info = store.create()
    store.append_turn(info.session_id, user_content="重启 nginx", assistant_content="⚠️ 待审批",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")
    store.append_turn(info.session_id, user_content="在吗", assistant_content="在")

    assert [m.content for m in store.history(info.session_id)] == ["在吗", "在"]
    assert len(store.messages(info.session_id)) == 4, "展示口仍要看到全部"


def test_history_window_returns_last_n_in_ascending_order(store):
    info = store.create()
    for i in range(5):
        store.append_turn(info.session_id, user_content=f"q{i}", assistant_content=f"a{i}")

    window = store.history(info.session_id, limit=4)

    assert [m.content for m in window] == ["q3", "a3", "q4", "a4"]


def test_pending_approvals_count_is_live(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")

    assert store.get(info.session_id).pending_approvals == 1
    assert [s.pending_approvals for s in store.list()] == [1]


# -- 回填 / 作废 -------------------------------------------------------------
def test_update_pending_backfills_final_answer(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="重启 nginx", assistant_content="⚠️ 待审批",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")

    assert store.update_pending("ap1", content="已重启完成") is True

    msgs = store.messages(info.session_id)
    assert msgs[1].content == "已重启完成"
    assert msgs[1].status == STATUS_FINAL
    assert msgs[1].seq == 2, "回填是 UPDATE 原地改，不占新 seq"
    assert [m.content for m in store.history(info.session_id)] == ["重启 nginx", "已重启完成"]


def test_update_pending_chained_approval_swaps_id(store):
    """链式审批：同一行推进到下一个 approval_id，而不是新开一行。"""
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️ 第一条",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")

    store.update_pending("ap1", content="⚠️ 第二条",
                         status=STATUS_PENDING_APPROVAL, next_approval_id="ap2")

    msgs = store.messages(info.session_id)
    assert len(msgs) == 2
    assert (msgs[1].approval_id, msgs[1].status) == ("ap2", STATUS_PENDING_APPROVAL)


def test_update_pending_unknown_approval_returns_false(store):
    info = store.create()
    assert store.update_pending("nope", content="x") is False


def test_update_pending_abandoned(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")

    assert store.update_pending("ap1", content="⚠️", status=STATUS_ABANDONED) is True
    assert store.messages(info.session_id)[1].status == STATUS_ABANDONED


def test_reconcile_pending_with_empty_set_abandons_all(store):
    """开库时传空集合：新进程里任何 pending 行必然是孤儿（审批不跨重启存活）。"""
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")

    assert store.reconcile_pending(set()) == 1
    assert store.messages(info.session_id)[1].status == STATUS_ABANDONED
    assert store.get(info.session_id).pending_approvals == 0


def test_reconcile_pending_keeps_live_ids(store):
    """运行期判据：审批被 TTL/容量淘汰后，对应行也要作废（只看启动时不够）。"""
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap-live")
    other = store.create()
    store.append_turn(other.session_id, user_content="y", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap-dead")

    assert store.reconcile_pending({"ap-live"}) == 1

    assert store.messages(info.session_id)[1].status == STATUS_PENDING_APPROVAL
    assert store.messages(other.session_id)[1].status == STATUS_ABANDONED


def test_reconcile_pending_is_idempotent(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")

    assert store.reconcile_pending(set()) == 1
    assert store.reconcile_pending(set()) == 0, "已作废的行不该被再次统计"


# -- 清理 -------------------------------------------------------------------
def test_purge_expired_by_ttl(store):
    clock = store._clock
    old = store.create(user="old")
    clock.advance(1800)
    fresh = store.create(user="fresh")  # 此刻 old 才 1800s，未过期
    clock.advance(1801)                 # old 累计 3601s > ttl=3600

    removed = store.purge_expired()

    assert removed == 1
    assert store.get(old.session_id) is None
    assert store.get(fresh.session_id) is not None
    assert store.messages(old.session_id) == [], "消息一并清理"


def test_purge_by_capacity_evicts_oldest(store):
    clock = store._clock
    ids = []
    for i in range(4):  # max_sessions=3
        ids.append(store.create(user=f"u{i}").session_id)
        clock.advance(1)

    store.purge_expired()

    assert store.get(ids[0]) is None, "最旧的被淘汰"
    assert [s.session_id for s in store.list()] == list(reversed(ids[1:]))


def test_purge_keeps_recently_updated_session(store):
    """活跃会话不该因为建得早而被淘汰——淘汰看的是 updated_at，不是 created_at。"""
    clock = store._clock
    kept = store.create()
    clock.advance(1)
    store.create()
    clock.advance(1)
    store.create()  # 已到容量上限 3
    clock.advance(1)
    store.append_turn(kept.session_id, user_content="still here", assistant_content="yes")
    clock.advance(1)

    store.create()  # 第 4 个 → 淘汰 updated_at 最旧的那个

    assert store.get(kept.session_id) is not None
    assert len(store.list()) == 3


def test_append_turn_to_unknown_session_is_rejected(tmp_path):
    """内存实现显式拒绝；SQLite 版因无外键不会报错——故只测内存实现。"""
    store = InMemorySessionStore()
    with pytest.raises(SessionError):
        store.append_turn("nope", user_content="a", assistant_content="b")
