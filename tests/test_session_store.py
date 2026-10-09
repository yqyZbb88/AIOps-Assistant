"""会话存储：SQLite 与内存两套实现跑同一组契约测试（离线）。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from src.interfaces.session import (
    STATUS_ABANDONED,
    STATUS_FINAL,
    STATUS_PENDING_APPROVAL,
    STATUS_UNKNOWN,
    SessionError,
    SessionInfo,
    is_owner,
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


# -- 会话名（**纯标签**，不参与选中会话） ---------------------------------------
def test_create_with_name_stores_it_as_label(store):
    info = store.create(user="ops", name="生产排查")

    assert info.name == "生产排查"
    assert store.get(info.session_id).name == "生产排查"
    assert [s.name for s in store.list()] == ["生产排查"]


def test_unnamed_session_has_empty_name(store):
    assert store.create(user="ops").name == ""


def test_duplicate_names_are_allowed(store):
    """名字**不唯一**：它只是给人看的标签，不参与选中会话。

    强制唯一只会让人想开第二段同名对话时被迫改名。
    """
    a = store.create(user="ops", name="生产排查")
    b = store.create(user="ops", name="生产排查")

    assert a.session_id != b.session_id
    assert len(store.list()) == 2
    assert {s.name for s in store.list()} == {"生产排查"}


def test_same_name_different_user_is_also_allowed(store):
    a = store.create(user="alice", name="test_1")
    b = store.create(user="bob", name="test_1")

    assert a.session_id != b.session_id


def test_create_never_reuses_an_existing_session(store):
    """**关键**：同名不会复用已有会话。

    曾经（v2）名字是「别名」，重名会接回旧会话。降级为标签后必须**每次都是新会话**——
    否则调用方以为开了一段新对话、实际接到了旧的，那正是这次要消除的意外。
    """
    first = store.create(user="ops", name="test_1")
    second = store.create(user="ops", name="test_1")

    assert second.session_id != first.session_id
    assert len(store.list()) == 2


def test_name_can_be_reused_after_delete(store):
    first = store.create(user="ops", name="test_1")
    store.delete(first.session_id)

    second = store.create(user="ops", name="test_1")

    assert second.session_id != first.session_id
    assert store.get(second.session_id).name == "test_1"


# -- 健康状态：反映「最近一次操作」，而不是「历史上有没有失败过」 --------------
def _flaky_db(db, *, failures: int, error: Exception):
    """让 `db.connect()` 给出的连接在**头 N 次写语句**上抛错，其余放行。

    必须打在**连接**这一层，不能替换 `db.execute`：重试循环在 `Database.execute`
    **内部**、包着 `conn.execute`；替换掉整个 `db.execute` 等于把重试也一起换掉了，
    测出来的就是「没有重试」——我第一版正是这么写错的。
    """
    real_connect = db.connect
    remaining = {"n": failures}

    class Flaky:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=()):
            if remaining["n"] > 0 and sql.lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE")
            ):
                remaining["n"] -= 1
                raise error
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def patched():
        with real_connect() as conn:
            yield Flaky(conn)

    return patched, remaining


def test_health_recovers_after_a_successful_operation(tmp_path, monkeypatch):
    """**一次瞬时失败不能把 healthy 永久钉死。**

    否则一次几秒的锁（典型成因是外部查看器占着库）过后，`/health` 会永远显示
    degraded，而库其实好好的——那个字段也就没人信了，比不报还糟。
    """
    store = SqliteSessionStore(open_database(tmp_path / "x.db"))
    patched, _ = _flaky_db(store.db, failures=99, error=sqlite3.OperationalError("boom"))
    monkeypatch.setattr(store.db, "connect", patched)

    with pytest.raises(SessionError):
        store.create(user="ops")
    assert store.healthy is False
    assert store.last_error

    monkeypatch.undo()  # 恢复正常

    store.create(user="ops")
    assert store.healthy is True, "成功一次就该恢复"
    assert store.last_error is None


def test_transient_lock_is_retried_instead_of_losing_the_turn(tmp_path, monkeypatch):
    """撞一次瞬时锁不该丢掉整轮会话——退避重试应当把它救回来。"""
    import src.storage.db as dbmod

    monkeypatch.setattr(dbmod, "_TRANSIENT_RETRY_DELAYS", (0.0, 0.0))
    store = SqliteSessionStore(open_database(tmp_path / "x.db"))
    patched, remaining = _flaky_db(
        store.db, failures=1, error=sqlite3.OperationalError("database is locked")
    )
    monkeypatch.setattr(store.db, "connect", patched)

    info = store.create(user="ops")

    assert info.session_id, "重试之后应当成功"
    assert remaining["n"] == 0
    assert store.healthy is True


def test_permanent_error_still_degrades(tmp_path, monkeypatch):
    """不是瞬时的错误（比如表不存在）不该被重试掩盖，仍要如实降级。"""
    store = SqliteSessionStore(open_database(tmp_path / "x.db"))
    patched, remaining = _flaky_db(
        store.db, failures=99, error=sqlite3.OperationalError("no such table: nope")
    )
    monkeypatch.setattr(store.db, "connect", patched)

    with pytest.raises(SessionError):
        store.create(user="ops")

    assert store.healthy is False
    assert remaining["n"] == 98, "非瞬时错误只该试一次"


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

    assert store.reconcile_pending(set()) == (1, 0), "没提交过决策 → 作废"
    assert store.messages(info.session_id)[1].status == STATUS_ABANDONED
    assert store.get(info.session_id).pending_approvals == 0


def test_reconcile_pending_keeps_live_ids(store):
    """运行期判据：审批被 TTL/容量淘汰后，对应行也要收敛（只看启动时不够）。"""
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap-live")
    other = store.create()
    store.append_turn(other.session_id, user_content="y", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap-dead")

    assert store.reconcile_pending({"ap-live"}) == (1, 0)

    assert store.messages(info.session_id)[1].status == STATUS_PENDING_APPROVAL
    assert store.messages(other.session_id)[1].status == STATUS_ABANDONED


def test_reconcile_pending_is_idempotent(store):
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap1")

    assert store.reconcile_pending(set()) == (1, 0)
    assert store.reconcile_pending(set()) == (0, 0), "已收敛的行不该被再次统计"


# -- 决策记录：区分「一定没执行」与「可能已经执行」 ---------------------------
def test_reconcile_marks_decided_orphans_as_unknown(store):
    """**这套机制存在的全部理由。**

    `decide()` 先 `pop()` 掉审批项、再 `resume()` 执行命令。进程若死在两者之间，
    命令**可能已经执行过**——标成 `abandoned`（未执行）会让审计结论与事实相反，
    方向恰是 fail-safe 的反面。有决策记录 → 必须标 `unknown`。
    """
    info = store.create()
    store.append_turn(info.session_id, user_content="重启 nginx", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap-done")
    store.record_decision("ap-done", session_id=info.session_id, action="approve")

    assert store.reconcile_pending(set()) == (0, 1)
    assert store.messages(info.session_id)[1].status == STATUS_UNKNOWN
    assert store.get(info.session_id).pending_approvals == 0, "结果未知也不再是「等你批」"


def test_reconcile_keeps_undecided_orphans_as_abandoned(store):
    """没有决策记录 = 决策从没提交过 = 命令**一定**没执行。两类不能混。"""
    info = store.create()
    store.append_turn(info.session_id, user_content="x", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap-undone")

    assert store.reconcile_pending(set()) == (1, 0)
    assert store.messages(info.session_id)[1].status == STATUS_ABANDONED


def test_reconcile_handles_both_kinds_in_one_pass(store):
    """同一批孤儿里两类混在一起——先标 unknown 再标 abandoned，别互相覆盖。"""
    done = store.create()
    store.append_turn(done.session_id, user_content="a", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap-done")
    store.record_decision("ap-done", session_id=done.session_id, action="approve")
    undone = store.create()
    store.append_turn(undone.session_id, user_content="b", assistant_content="⚠️",
                      status=STATUS_PENDING_APPROVAL, approval_id="ap-undone")

    assert store.reconcile_pending(set()) == (1, 1)

    assert store.messages(done.session_id)[1].status == STATUS_UNKNOWN
    assert store.messages(undone.session_id)[1].status == STATUS_ABANDONED


def test_decided_ids_are_reported(store):
    store.record_decision("ap-1", session_id="s", action="approve")
    store.record_decision("ap-2", session_id="s", action="reject")

    assert store.decided_approval_ids() == {"ap-1", "ap-2"}


def test_decision_records_are_dropped_with_their_session(store):
    """决策记录只为「重启后判 unknown」而存在，会话没了就没人再读它。"""
    info = store.create()
    store.record_decision("ap-1", session_id=info.session_id, action="approve")
    store._clock.advance(3601)  # 超出 fixture 的 ttl=3600

    store.purge_expired()

    assert store.decided_approval_ids() == set()


def test_delete_drops_decisions_of_that_session(store):
    """用户主动删会话时，它的决策记录必须一起走（两套实现都要做到）。

    决策表没有外键，也没人会因为会话消失回头清理它；只靠状态日志清理按 TTL 兜底的话，
    孤儿行会一直挂到过期——而 delete 是用户随时能触发的动作，不是低频路径。
    """
    doomed = store.create()
    kept = store.create()
    store.record_decision("ap-doomed", session_id=doomed.session_id, action="approve")
    store.record_decision("ap-kept", session_id=kept.session_id, action="reject")

    assert store.delete(doomed.session_id) is True

    assert store.decided_approval_ids() == {"ap-kept"}


# -- 归属（API/core 传入身份；None 仅限可信内部调用，不筛） --------------
def test_list_filters_by_user(store):
    store.create(user="alice")
    store.create(user="bob")
    store.create(user="alice")

    assert len(store.list(user="alice")) == 2
    assert [s.user for s in store.list(user="bob")] == ["bob"]
    assert len(store.list()) == 3, "user=None → 不筛，行为与改造前一致"


def test_list_filter_applies_before_limit(store):
    """过滤必须下推到 LIMIT **之前**。

    故意让 alice 是**最旧**的一条、bob 是最新的两条（总数 3 恰好等于 fixture 的
    `max_sessions`，避开容量淘汰的干扰）。先取 `limit=1` 条再筛的话，取到的是最新的
    bob、筛完为空——「最近 1 条」静默变成「一条都没有」。
    """
    store.create(user="alice")
    store.create(user="bob")
    store.create(user="bob")

    got = store.list(user="alice", limit=1)

    assert len(got) == 1
    assert got[0].user == "alice"


def test_delete_rejects_other_owner(store):
    """非 owner 的删除必须**什么都不做**——包括不能抹掉消息。

    SQLite 版原先先删 messages 再删 sessions：守卫若只加在 sessions 那条 SQL 上，
    一次未授权的删除就会抹掉别人的对话正文，而会话行还在。
    """
    info = store.create(user="alice")
    store.append_turn(info.session_id, user_content="机密内容", assistant_content="收到")

    assert store.delete(info.session_id, user="bob") is False

    assert store.get(info.session_id) is not None, "会话行必须还在"
    assert len(store.messages(info.session_id)) == 2, "消息也不能被抹掉"


def test_delete_with_matching_owner(store):
    info = store.create(user="alice")
    store.append_turn(info.session_id, user_content="a", assistant_content="b")

    assert store.delete(info.session_id, user="alice") is True

    assert store.get(info.session_id) is None
    assert store.messages(info.session_id) == []


def test_delete_without_user_does_not_check(store):
    info = store.create(user="alice")
    assert store.delete(info.session_id) is True, "user=None → 不校验，行为与改造前一致"


def test_delete_rejects_unowned_row(store):
    """存量行 `user` 为空 → **任何身份都不匹配**（2026-09-12 用户选的「一律拒绝」）。"""
    info = store.create()  # user=""

    assert store.delete(info.session_id, user="alice") is False
    assert store.get(info.session_id) is not None


# -- 改名 -------------------------------------------------------------------
def test_rename_changes_the_label(store):
    info = store.create(user="alice", name="旧名")

    assert store.rename(info.session_id, name="新名", user="alice") is True
    assert store.get(info.session_id).name == "新名"


def test_rename_rejects_other_owner(store):
    info = store.create(user="alice", name="旧名")

    assert store.rename(info.session_id, name="新名", user="bob") is False
    assert store.get(info.session_id).name == "旧名", "别人的标签一个字都不能动"


def test_rename_unknown_returns_false(store):
    assert store.rename("nope", name="新名") is False


def test_rename_does_not_bump_updated_at(store):
    """改名不是「会话活动」。

    刷 `updated_at` 会让 `GET /sessions` 的排序与 TTL 过期时机跟着一次纯标签操作跳动，
    而这两处都该反映「最后一句话是什么时候说的」。
    """
    info = store.create(user="alice")
    before = store.get(info.session_id).updated_at
    store._clock.advance(60)

    store.rename(info.session_id, name="新名")

    assert store.get(info.session_id).updated_at == before


# -- create 的 user 归一 -----------------------------------------------------
def test_create_normalises_none_user_to_empty_string(store):
    """可信内部调用可传 `None`；HTTP 业务请求始终传已验证身份。

    不归一的话会 `INSERT ... user=NULL` 并构造出 `SessionInfo(user=None)`，
    而读路径 `_row_to_session` 有 `or ""` 兜底——同一个字段两条路径两种类型。
    """
    info = store.create(user=None)

    assert info.user == ""
    assert store.get(info.session_id).user == ""
    assert store.list()[0].user == ""


# -- is_owner（纯函数：api 与 core 共用的**唯一**归属判定实现） ----------------
def test_is_owner_allows_everything_when_auth_is_off():
    """`identity is None` = 鉴权未启用 → 不校验，行为与改造前一致。"""
    assert is_owner(SessionInfo(session_id="s", user="alice"), None) is True
    assert is_owner(SessionInfo(session_id="s", user=""), None) is True


def test_is_owner_requires_exact_match():
    info = SessionInfo(session_id="s", user="alice")

    assert is_owner(info, "alice") is True
    assert is_owner(info, "bob") is False
    assert is_owner(info, "Alice") is False, "身份是精确串：大小写敏感，不做任何归一"


def test_is_owner_rejects_unowned_row_for_any_identity():
    """「一律拒绝」：`user` 为空的存量行，**任何**非 None 身份都不匹配。"""
    unowned = SessionInfo(session_id="s", user="")

    assert is_owner(unowned, "alice") is False
    assert is_owner(unowned, "bob") is False


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


def test_append_turn_to_unknown_session_is_rejected(store):
    """两种存储都必须拒绝未知或并发删除的会话，不能写入孤立消息。"""
    with pytest.raises(SessionError):
        store.append_turn("nope", user_content="a", assistant_content="b")
