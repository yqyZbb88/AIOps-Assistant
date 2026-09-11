"""ApprovalStore：有界、单次性、会过期的待审批缓冲（离线，clock 可注入）。"""
from __future__ import annotations

import pytest

from src.storage.approval import ApprovalStore


class FakeClock:
    """可控时钟：用于离线验证过期逻辑，不依赖真实等待。"""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _rec(approval_id: str, **extra):
    return {"approval_id": approval_id, "thread_id": f"t-{approval_id}", "actions": [], **extra}


def test_add_get_list_pop():
    store = ApprovalStore()
    store.add(_rec("a"))
    store.add(_rec("b"))

    assert len(store) == 2
    assert store.get("a")["thread_id"] == "t-a"
    # list 新的在前
    assert [r["approval_id"] for r in store.list()] == ["b", "a"]

    assert store.pop("b")["approval_id"] == "b"
    assert len(store) == 1
    assert store.get("b") is None


def test_pop_is_single_use():
    store = ApprovalStore()
    store.add(_rec("a"))
    assert store.pop("a") is not None
    assert store.pop("a") is None, "同一 id 不能取出两次（防重放）"


def test_get_unknown_returns_none():
    assert ApprovalStore().get("nope") is None
    assert ApprovalStore().pop("nope") is None


def test_maxlen_evicts_oldest_and_returns_it():
    store = ApprovalStore(maxlen=2)
    store.add(_rec("a"))
    store.add(_rec("b"))
    evicted = store.add(_rec("c"))

    assert evicted is not None and evicted["approval_id"] == "a"
    assert store.get("a") is None, "被淘汰的审批项不得再可批准"
    assert [r["approval_id"] for r in store.list()] == ["c", "b"]
    assert len(store) == 2


def test_evicted_record_carries_thread_id():
    """回归：淘汰时返回的必须是**整条记录**，调用方要靠 thread_id 删 checkpoint 线程。

    原先只返回 {"approval_id": ...}，调用方拿到后 `_delete_thread(None)` 静默无操作，
    线程照旧泄漏——docstring 承诺的「返回是为了让调用方 delete_thread」根本没兑现。
    """
    store = ApprovalStore(maxlen=1)
    store.add(_rec("a"))

    evicted = store.add(_rec("b"))

    assert evicted is not None
    assert evicted["thread_id"] == "t-a", "缺 thread_id 则调用方无法清理其线程"


def test_purge_expired_returns_records_and_drops_them():
    clock = FakeClock()
    store = ApprovalStore(ttl_seconds=60, clock=clock)
    store.add(_rec("old"))
    clock.advance(61)
    store.add(_rec("new"))

    expired = store.purge_expired()

    assert [r["approval_id"] for r in expired] == ["old"]
    assert store.get("old") is None
    assert store.get("new") is not None


def test_get_expired_returns_none():
    clock = FakeClock()
    store = ApprovalStore(ttl_seconds=30, clock=clock)
    store.add(_rec("a"))
    clock.advance(31)

    assert store.get("a") is None, "过期即作废，绝不自动放行"
    assert len(store) == 0


def test_pop_expired_returns_none():
    clock = FakeClock()
    store = ApprovalStore(ttl_seconds=30, clock=clock)
    store.add(_rec("a"))
    clock.advance(31)
    assert store.pop("a") is None


def test_list_purges_expired():
    clock = FakeClock()
    store = ApprovalStore(ttl_seconds=10, clock=clock)
    store.add(_rec("a"))
    clock.advance(11)
    assert store.list() == []


def test_ttl_and_maxlen_clamped_to_at_least_one():
    store = ApprovalStore(maxlen=0, ttl_seconds=0)
    assert store.maxlen == 1
    assert store.ttl_seconds == 1


def test_created_at_is_stamped_when_missing():
    store = ApprovalStore()
    store.add({"approval_id": "a", "thread_id": "t"})
    assert store.get("a")["created_at"]
