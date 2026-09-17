"""Agent 写命令的人工审批链路（真 graph + 脚本化假模型，全程离线）。

这里不碰任何真实 LLM / SSH：用一个脚本化的 BaseChatModel 驱动真实的
create_agent + HumanInTheLoopMiddleware + InMemorySaver，验证的正是最容易出错、
且无法靠 mock 覆盖的部分——中断与续跑的真实语义。

覆盖的坑（都经实测确认）：
- 中断不是异常，reply() 必须先判 __interrupt__，否则返回「抱歉…」把待审批伪装成失败；
- 有 checkpointer 不传 thread_id → ValueError（会被 ChatService 吞掉）；
- 决策负载不合法会**永久毒化**线程 → decide() 必须先校验；
- 审批 id 单次性 → 重复 decide 必须报错且不二次执行；
- 批准前命令**一次都不能执行**（fail-safe）。
"""
from __future__ import annotations

import pytest

# 顶层就 import langchain 在本测试里是刻意的：本文件专门验证真 graph 行为。
# 需要 langchain/langgraph（可选依赖①）；缺失时整个文件跳过，而非报错。
pytest.importorskip("langchain")
pytest.importorskip("langgraph")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402

from src.agent.assistant import ALLOWED_APPROVAL_DECISIONS, OpsAssistant  # noqa: E402
from src.interfaces.approval import (  # noqa: E402
    ApprovalError,
    ApprovalUnavailableError,
)
from src.interfaces.session import TurnResult  # noqa: E402
from src.storage.approval import ApprovalStore  # noqa: E402


class ScriptedChatModel(BaseChatModel):
    """按脚本依次返回固定消息的假模型（脚本用尽后重复最后一条）。

    必须自定义而非用 GenericFakeChatModel：后者未实现 bind_tools，
    而 create_agent 建 agent 时一定会调用它。
    """

    responses: list
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        idx = min(self.cursor, len(self.responses) - 1)
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=self.responses[idx])])


def _tool_call(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def _build_assistant(
    monkeypatch, responses, *, fake_actions, diag=None, approvals=None, ledger=None
):
    """构建挂了审批中间件的 OpsAssistant（模型被替换为脚本化假模型）。"""
    from src.agent import assistant as assistant_mod

    model = ScriptedChatModel(responses=responses)
    monkeypatch.setattr(assistant_mod, "get_model", lambda: model)
    return OpsAssistant(
        monitor=_monitor(),
        diag=diag,
        agent_actions=fake_actions,
        approvals=approvals if approvals is not None else ApprovalStore(),
        ledger=ledger,
    )


class RecordingLedger:
    """记录决策落盘与对账调用的假 ledger（也可换成真的 InMemorySessionStore）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, str | None, str, str]] = []
        self.reconciled: list[set[str]] = []

    def record_decision(
        self, approval_id: str, *, session_id: str | None, action: str, decided_by: str = ""
    ) -> None:
        self.records.append((approval_id, session_id, action, decided_by))

    def reconcile_pending(self, live_approval_ids):
        self.reconciled.append({str(x) for x in live_approval_ids})
        return 0, 0


def _monitor():
    from tests.conftest import FakeMonitor

    return FakeMonitor()


@pytest.fixture
def approvals() -> ApprovalStore:
    return ApprovalStore()


# ---------------------------------------------------------------------------
# 中断：命令不得执行
# ---------------------------------------------------------------------------
def test_reply_interrupts_and_does_not_execute(monkeypatch, fake_actions, approvals):
    """模型请求写命令 → 返回待审批提示、源未被调用、待审批列表有 1 条。"""
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"})],
        fake_actions=fake_actions,
        approvals=approvals,
    )

    reply = agent.reply("帮我重启 nginx")

    assert "待人工审批" in reply, reply
    assert "restart_service" in reply and "nginx" in reply
    assert fake_actions.calls == [], "批准前绝不能执行写命令"
    pending = agent.pending_approvals()
    assert len(pending) == 1
    assert pending[0].actions[0]["name"] == "restart_service"
    assert pending[0].allowed_decisions == ALLOWED_APPROVAL_DECISIONS


# -- 待审批项的**上下文**（归属隔离与 approval_decisions 都靠它） -------------
def test_pending_records_session_and_user_from_reply_turn_ctx(monkeypatch, fake_actions, approvals):
    """中断登记时必须把会话与身份一并记下。

    Agent 自己既不知道当前是哪个会话、也不知道调用者是谁——只能由 `reply_turn_ctx`
    送进来。`/approvals` 的身份隔离与 `approval_decisions.session_id` 都依赖它。
    """
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"})],
        fake_actions=fake_actions,
        approvals=approvals,
    )

    turn = agent.reply_turn_ctx("重启 nginx", session_id="s-1", user="alice")

    rec = approvals.get(turn.approval_id)
    assert rec is not None
    assert rec["session_id"] == "s-1"
    assert rec["user"] == "alice"
    assert agent.pending_approvals()[0].user == "alice", "对外暴露的 PendingApproval 也要带"


def test_pending_context_defaults_to_empty(monkeypatch, fake_actions, approvals):
    """走旧入口（`reply_turn` / `reply`）时不带上下文——不报错，只是归属为空。

    兼容线的价值：既有调用方与各测试假对象一行都不用改。
    """
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"})],
        fake_actions=fake_actions,
        approvals=approvals,
    )

    turn = agent.reply_turn("重启 nginx")

    rec = approvals.get(turn.approval_id)
    assert rec["session_id"] is None
    assert rec["user"] == ""


def test_reply_turn_is_a_thin_forwarder(monkeypatch, fake_actions, approvals):
    """`reply_turn` 只是 `reply_turn_ctx` 的简化形式——**只有一条 invoke 路径**。

    用 spy 钉死这个转发关系：将来若有人把主体搬回 `reply_turn`、留下两条路径，
    这条会立刻红。
    """
    agent = _build_assistant(
        monkeypatch, [AIMessage(content="x")], fake_actions=fake_actions, approvals=approvals
    )
    seen: dict = {}

    def spy(message, *, history=(), session_id=None, user=None):
        seen.update(message=message, session_id=session_id, user=user)
        return TurnResult(text="ok")

    monkeypatch.setattr(agent, "reply_turn_ctx", spy)

    assert agent.reply_turn("hi").text == "ok"
    assert seen == {"message": "hi", "session_id": None, "user": None}


# -- 决策落盘：位置就是这套机制的全部要害 -----------------------------------
def test_decide_records_the_decision(monkeypatch, fake_actions, approvals):
    """`decide()` 必须落一条决策记录，且带上会话与决策类型。"""
    ledger = RecordingLedger()
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"}),
         AIMessage(content="已重启 nginx")],
        fake_actions=fake_actions,
        approvals=approvals,
        ledger=ledger,
    )
    turn = agent.reply_turn_ctx("重启 nginx", session_id="s-1", user="alice")

    agent.decide(turn.approval_id, action="approve")

    assert ledger.records == [(turn.approval_id, "s-1", "approve", "alice")]


def test_decision_is_recorded_before_the_command_runs(monkeypatch, fake_actions, approvals):
    """**落盘必须发生在 resume 之前**——这是整套机制存在的理由。

    模拟「审批项已取走、命令还没跑完，进程死了」：让 resume 抛错。
    只要记录已经写下，重启后的 reconcile 就能把那一轮标成「结果未知」，
    而不是错误的「未执行」（命令可能其实执行过了）。
    """
    ledger = RecordingLedger()
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"})],
        fake_actions=fake_actions,
        approvals=approvals,
        ledger=ledger,
    )
    turn = agent.reply_turn_ctx("重启 nginx", session_id="s-7", user="alice")

    def boom(*a, **k):
        raise RuntimeError("模拟：命令执行到一半进程死了")

    monkeypatch.setattr(agent.agent, "invoke", boom)

    with pytest.raises(RuntimeError):
        agent.decide(turn.approval_id, action="approve")

    assert ledger.records == [(turn.approval_id, "s-7", "approve", "alice")], "崩之前记录就该在了"
    assert approvals.get(turn.approval_id) is None, "审批项确实已被 pop 掉"


def test_decision_record_failure_aborts_without_running(monkeypatch, fake_actions, approvals):
    """记录写不进去 → **不执行命令**，审批项恢复后仍可重试。"""
    class BrokenLedger:
        def record_decision(self, *a, **k):
            raise RuntimeError("磁盘满了")

        def reconcile_pending(self, *a, **k):
            return 0, 0

    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"})],
        fake_actions=fake_actions,
        approvals=approvals,
        ledger=BrokenLedger(),
    )
    turn = agent.reply_turn_ctx("重启 nginx", session_id="s", user="alice")
    called: list = []
    monkeypatch.setattr(agent.agent, "invoke", lambda *a, **k: called.append(1))

    with pytest.raises(ApprovalUnavailableError):
        agent.decide(turn.approval_id, action="approve")

    assert called == [], "命令绝不能执行"
    assert approvals.get(turn.approval_id) is not None, "落盘失败后必须恢复，才能按提示重试"


def test_decision_record_failure_can_retry_exactly_once(monkeypatch, fake_actions, approvals):
    """瞬时落盘失败后，同一 id 重试成功，写命令总共只执行一次。"""
    class FlakyLedger(RecordingLedger):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        def record_decision(self, approval_id, *, session_id, action, decided_by=""):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("数据库暂时锁定")
            super().record_decision(
                approval_id,
                session_id=session_id,
                action=action,
                decided_by=decided_by,
            )

    ledger = FlakyLedger()
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"}),
         AIMessage(content="已重启 nginx")],
        fake_actions=fake_actions,
        approvals=approvals,
        ledger=ledger,
    )
    turn = agent.reply_turn_ctx("重启 nginx", session_id="s", user="alice")

    with pytest.raises(ApprovalUnavailableError):
        agent.decide(turn.approval_id, action="approve")

    outcome = agent.decide(turn.approval_id, action="approve")

    assert outcome.status == "completed"
    assert fake_actions.calls == [("restart_service", {"service": "nginx"})]
    assert ledger.records == [(turn.approval_id, "s", "approve", "alice")]
    assert approvals.get(turn.approval_id) is None, "成功消费后仍须保持单次性"


def test_crash_window_ends_up_unknown_not_abandoned(monkeypatch, fake_actions, approvals):
    """**端到端回归**：决策提交后崩溃 → 那一轮是「结果未知」，不是「未执行」。

    整条链路：中断登记 → decide（pop → 落决策 → resume 崩）→ 新进程 reconcile。
    """
    from src.interfaces.session import STATUS_PENDING_APPROVAL, STATUS_UNKNOWN
    from src.storage.session import InMemorySessionStore

    store = InMemorySessionStore()
    info = store.create(user="alice")
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"})],
        fake_actions=fake_actions,
        approvals=approvals,
        ledger=store,
    )
    turn = agent.reply_turn_ctx("重启 nginx", session_id=info.session_id, user="alice")
    store.append_turn(
        info.session_id, user_content="重启 nginx", assistant_content="⚠️ 待人工审批",
        status=STATUS_PENDING_APPROVAL, approval_id=turn.approval_id,
    )

    monkeypatch.setattr(
        agent.agent, "invoke", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("崩"))
    )
    with pytest.raises(RuntimeError):
        agent.decide(turn.approval_id, action="approve")

    # 模拟重启：新进程里任何 pending 行都是孤儿
    abandoned, unknown = store.reconcile_pending(set())

    assert (abandoned, unknown) == (0, 1), "提交过决策就不能判成「未执行」"
    assert store.messages(info.session_id)[1].status == STATUS_UNKNOWN


def test_evicted_pending_is_reconciled_immediately(monkeypatch, fake_actions):
    """审批项被容量淘汰时，**当场**把会话里那轮收敛掉。

    不这么做的话它会一直显示「有一条命令等你批」，而 `/approvals` 里根本没有它——
    用户永远批不了，只能等下次重启。而 `_purge_expired` 恰好知道有没有真淘汰东西，
    所以对账只在真淘汰时发生，没淘汰的轮次零额外开销。
    """
    ledger = RecordingLedger()
    store = ApprovalStore(maxlen=1)
    # 两轮各触发一次中断（中断会停在工具执行之前，所以每轮只消耗一条脚本）
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"}),
         _tool_call("restart_service", {"service": "mysql"})],
        fake_actions=fake_actions,
        approvals=store,
        ledger=ledger,
    )

    agent.reply_turn_ctx("重启 nginx", session_id="s", user="alice")
    assert ledger.reconciled == [], "没淘汰就不该对账"

    agent.reply_turn_ctx("重启 mysql", session_id="s", user="alice")

    assert len(ledger.reconciled) == 1, "淘汰了就该对账一次"
    assert len(ledger.reconciled[-1]) == 1, "live 里只剩刚加的那条"


def test_reply_pending_notice_is_a_plain_string(monkeypatch, fake_actions):
    """/chat 契约不变：待审批时返回的仍是 str（调用方无需新增响应字段）。"""
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"})],
        fake_actions=fake_actions,
    )
    assert isinstance(agent.reply("重启 nginx"), str)


def test_no_write_tools_means_no_interrupt(monkeypatch):
    """未配置写命令时保持单轮无状态行为（不建 checkpointer）。"""
    from src.agent import assistant as assistant_mod

    monkeypatch.setattr(
        assistant_mod, "get_model", lambda: ScriptedChatModel(responses=[AIMessage(content="你好")])
    )
    agent = OpsAssistant(monitor=_monitor())
    assert agent.checkpointer is None
    assert agent.write_tools == []
    assert agent.reply("你好") == "你好"
    assert agent.pending_approvals() == []


# ---------------------------------------------------------------------------
# 审批：批准才执行
# ---------------------------------------------------------------------------
def test_approve_executes_exactly_once(monkeypatch, fake_actions, approvals):
    """批准后命令执行一次，参数为模型给出的语义参数，状态 completed。"""
    agent = _build_assistant(
        monkeypatch,
        [
            _tool_call("restart_service", {"service": "nginx"}),
            AIMessage(content="已为你重启 nginx"),
        ],
        fake_actions=fake_actions,
        approvals=approvals,
    )
    agent.reply("帮我重启 nginx")
    approval_id = agent.pending_approvals()[0].approval_id

    outcome = agent.decide(approval_id, action="approve")

    assert outcome.status == "completed"
    assert outcome.decision == "approve"
    assert outcome.reply == "已为你重启 nginx"
    assert fake_actions.calls == [("restart_service", {"service": "nginx"})]
    assert agent.pending_approvals() == []


def test_reject_does_not_execute(monkeypatch, fake_actions, approvals):
    """拒绝后命令绝不执行，但 Agent 仍给出回答。"""
    agent = _build_assistant(
        monkeypatch,
        [
            _tool_call("restart_service", {"service": "nginx"}),
            AIMessage(content="好的，已取消重启操作"),
        ],
        fake_actions=fake_actions,
        approvals=approvals,
    )
    agent.reply("帮我重启 nginx")
    approval_id = agent.pending_approvals()[0].approval_id

    outcome = agent.decide(approval_id, action="reject", reason="当前是业务高峰")

    assert outcome.status == "completed"
    assert fake_actions.calls == [], "被拒绝的命令绝不能执行"
    assert agent.pending_approvals() == []


def test_decide_is_single_use(monkeypatch, fake_actions, approvals):
    """审批 id 单次性：重复提交必须报错，且不会二次执行。"""
    agent = _build_assistant(
        monkeypatch,
        [
            _tool_call("restart_service", {"service": "nginx"}),
            AIMessage(content="已完成"),
        ],
        fake_actions=fake_actions,
        approvals=approvals,
    )
    agent.reply("重启 nginx")
    approval_id = agent.pending_approvals()[0].approval_id
    agent.decide(approval_id, action="approve")

    with pytest.raises(ApprovalError, match="未知"):
        agent.decide(approval_id, action="approve")
    assert len(fake_actions.calls) == 1, "重放不得导致二次执行"


def test_decide_unknown_id_raises(monkeypatch, fake_actions):
    agent = _build_assistant(
        monkeypatch, [_tool_call("restart_service", {"service": "nginx"})], fake_actions=fake_actions
    )
    with pytest.raises(ApprovalError, match="未知"):
        agent.decide("nope", action="approve")


def test_decide_rejects_unsupported_action(monkeypatch, fake_actions):
    """edit 之类不在允许集合内的决策，必须在进入 resume 前就被拒（否则毒化线程）。"""
    agent = _build_assistant(
        monkeypatch,
        [
            _tool_call("restart_service", {"service": "nginx"}),
            AIMessage(content="已完成"),
        ],
        fake_actions=fake_actions,
    )
    agent.reply("重启 nginx")
    approval_id = agent.pending_approvals()[0].approval_id

    with pytest.raises(ApprovalError, match="不支持"):
        agent.decide(approval_id, action="edit")

    # 关键：被拒后该审批项仍可正常批准（线程未被毒化）
    outcome = agent.decide(approval_id, action="approve")
    assert outcome.status == "completed"
    assert fake_actions.calls == [("restart_service", {"service": "nginx"})]


# ---------------------------------------------------------------------------
# 多写命令链 / 只读工具不打断
# ---------------------------------------------------------------------------
def test_sequential_writes_chain_approvals(monkeypatch, fake_actions, approvals):
    """连续两条写命令：批准第一条后触发第二个待审批项，复用同一线程。"""
    agent = _build_assistant(
        monkeypatch,
        [
            _tool_call("restart_service", {"service": "nginx"}, "c1"),
            _tool_call("restart_service", {"service": "mysql"}, "c2"),
            AIMessage(content="两个服务都已重启"),
        ],
        fake_actions=fake_actions,
        approvals=approvals,
    )
    agent.reply("依次重启 nginx 和 mysql")
    first_id = agent.pending_approvals()[0].approval_id

    outcome = agent.decide(first_id, action="approve")

    assert outcome.status == "pending_approval"
    assert outcome.next_approval_id is not None
    assert fake_actions.calls == [("restart_service", {"service": "nginx"})]
    second_id = agent.pending_approvals()[0].approval_id
    assert second_id != first_id

    final = agent.decide(second_id, action="approve")
    assert final.status == "completed"
    assert [c[1]["service"] for c in fake_actions.calls] == ["nginx", "mysql"]


def test_read_tool_does_not_interrupt(monkeypatch, fake_actions, fakediag, approvals):
    """只读诊断工具在同一条 Agent 里正常运行，只有写命令才中断。"""
    agent = _build_assistant(
        monkeypatch,
        [
            _tool_call("memory", {}, "c1"),
            AIMessage(content="内存 512MB 可用"),
        ],
        fake_actions=fake_actions,
        diag=fakediag,
        approvals=approvals,
    )

    reply = agent.reply("看看内存")

    assert reply == "内存 512MB 可用"
    assert agent.pending_approvals() == []
    assert fakediag.calls == [("memory", {})]


# ---------------------------------------------------------------------------
# 装配护栏
# ---------------------------------------------------------------------------
def test_write_tools_require_approval_store(monkeypatch, fake_actions):
    """有写工具却没有审批存储 → 构造即失败（写命令必须可审批）。"""
    from src.agent import assistant as assistant_mod

    monkeypatch.setattr(
        assistant_mod, "get_model", lambda: ScriptedChatModel(responses=[AIMessage(content="x")])
    )
    with pytest.raises(ValueError, match="approval store"):
        OpsAssistant(monitor=_monitor(), agent_actions=fake_actions, approvals=None)


def test_build_agent_rejects_interrupt_without_checkpointer():
    """编程错误护栏：有中断策略却没 checkpointer，应立刻报错而非静默挂起。"""
    from src.agent.assistant import _build_agent

    with pytest.raises(ValueError, match="checkpointer"):
        _build_agent(
            model=ScriptedChatModel(responses=[AIMessage(content="x")]),
            tools=[],
            system_prompt="x",
            name="t",
            interrupt_on={"restart_service": {"allowed_decisions": ["approve", "reject"]}},
            checkpointer=None,
        )


def test_checkpointer_created_only_with_write_tools(monkeypatch, fake_actions):
    agent = _build_assistant(
        monkeypatch, [_tool_call("restart_service", {"service": "nginx"})], fake_actions=fake_actions
    )
    assert agent.checkpointer is not None


# ---------------------------------------------------------------------------
# 端到端装配：容器 → 工厂 → OpsAssistant → 审批（验证接线，而非单件）
# ---------------------------------------------------------------------------
AGENT_ACTIONS_YAML = """
enabled: true
target:
  host: 10.0.0.9
  username: ops
  password: "placeholder"
commands:
  restart_service:
    summary: 重启服务
    template: "systemctl restart {service}"
    sudo: true
    params:
      service: { type: str, pattern: "^[a-z]+$", description: 服务名 }
"""


def test_container_wiring_drives_full_approval_flow(monkeypatch, tmp_path):
    """启用写命令后，容器装配出的 Agent 能走完：聊天 → 待审批 → 批准 → 执行。

    这是唯一覆盖「容器 → assistant_factory → OpsAssistant → 工具 + 审批」整条接线的用例：
    单件测试全绿但接线接错（例如工厂没把 source 传下去）时，只有这里会红。
    """
    from src.agent import assistant as assistant_mod
    from src.container import Container
    from src.utils.config_loader import Settings

    cfg_file = tmp_path / "agent_actions.yaml"
    cfg_file.write_text(AGENT_ACTIONS_YAML, encoding="utf-8")
    monkeypatch.setenv("OPS_AGENT_ACTIONS_FILE", str(cfg_file))

    calls: list[tuple[str, dict]] = []

    def fake_exec(endpoint, command, sudo=False, output_limit=4000):
        calls.append((command, {"sudo": sudo, "host": endpoint.host}))
        return 0, "restarted", ""

    monkeypatch.setattr("src.plugins.common.ssh.exec_command", fake_exec)

    model = ScriptedChatModel(
        responses=[
            _tool_call("restart_service", {"service": "nginx"}),
            AIMessage(content="已重启 nginx"),
        ]
    )
    monkeypatch.setattr(assistant_mod, "get_model", lambda: model)

    container = Container(settings=Settings())
    assert container.agent_actions() is not None, "写命令源应已装配"

    agent = container._assistant_factory()

    # 1) 聊天 → 中断，命令未执行
    reply = agent.reply("帮我重启 nginx")
    assert "待人工审批" in reply
    assert calls == [], "审批前绝不能执行"

    # 2) 批准 → 真正经 SSH 执行（endpoint 来自配置，不是模型）
    approval_id = agent.pending_approvals()[0].approval_id
    outcome = agent.decide(approval_id, action="approve")

    assert outcome.status == "completed"
    assert len(calls) == 1
    command, meta = calls[0]
    assert command == "systemctl restart nginx"
    assert meta["sudo"] is True, "sudo 必须来自目录声明"
    assert meta["host"] == "10.0.0.9", "主机必须来自配置，模型无从指定"
    assert outcome.reply == "已重启 nginx"


def test_container_wiring_injects_write_addendum(monkeypatch, tmp_path):
    """启用写命令后系统提示词应带写能力说明；未启用则不带。"""
    from src.agent import assistant as assistant_mod
    from src.agent.prompts import WRITE_TOOLS_ADDENDUM
    from src.container import Container
    from src.utils.config_loader import Settings

    monkeypatch.setattr(
        assistant_mod, "get_model", lambda: ScriptedChatModel(responses=[AIMessage(content="x")])
    )

    # 未启用 → 不带写能力说明。用临时关闭的配置，**不依赖仓库当前开关**
    # （仓库 2026-09-10 起为 true；若这里读仓库文件，开关一改用例就红）
    cfg_off = tmp_path / "agent_actions_off.yaml"
    cfg_off.write_text(
        AGENT_ACTIONS_YAML.replace("enabled: true", "enabled: false"), encoding="utf-8"
    )
    monkeypatch.setenv("OPS_AGENT_ACTIONS_FILE", str(cfg_off))
    plain = Container(settings=Settings())._assistant_factory()
    assert WRITE_TOOLS_ADDENDUM not in plain.system_prompt
    assert plain.checkpointer is None

    # 启用 → 带写能力说明
    cfg_file = tmp_path / "agent_actions.yaml"
    cfg_file.write_text(AGENT_ACTIONS_YAML, encoding="utf-8")
    monkeypatch.setenv("OPS_AGENT_ACTIONS_FILE", str(cfg_file))
    enabled = Container(settings=Settings())._assistant_factory()
    assert WRITE_TOOLS_ADDENDUM in enabled.system_prompt
    assert enabled.checkpointer is not None


# ---------------------------------------------------------------------------
# 回归：本轮回答的取法（多轮的前置条件）
# ---------------------------------------------------------------------------
def test_last_ai_content_ignores_history(monkeypatch, fake_actions):
    """回归：传了历史后，本轮 AI content 为空时**不得**返回上一轮的旧回答。

    实测确认 `response["messages"]` 是**整条线程的累积 state**（输入 3 条 → 返回 4 条，
    且首条就是输入首条），所以无脑反向扫全部消息会捞到历史里的 AI 文本返回给用户——
    静默串味，比返回兜底文案更糟，因为它看起来像个正常答案。
    修复前本用例返回「第一轮的旧回答：CPU 45%」。
    """
    from langchain_core.messages import AIMessage as _AI, HumanMessage as _Human

    from src.agent.assistant import _last_ai_content

    agent = _build_assistant(
        monkeypatch, [AIMessage(content="")], fake_actions=fake_actions
    )
    response = agent.agent.invoke(
        {"messages": [
            _Human("第一轮：CPU 怎么样"),
            _AI("第一轮的旧回答：CPU 45%"),
            _Human("第二轮：那内存呢"),
        ]},
        config={"configurable": {"thread_id": "t-history"}},
    )

    assert _last_ai_content(response) == "抱歉，我暂时无法回答这个问题。"


def test_last_ai_content_still_reads_current_turn(monkeypatch, fake_actions):
    """止步于 human 不能把本轮回答也挡掉——这是上一条的对照组。"""
    from langchain_core.messages import AIMessage as _AI, HumanMessage as _Human

    from src.agent.assistant import _last_ai_content

    agent = _build_assistant(
        monkeypatch, [AIMessage(content="本轮的新回答")], fake_actions=fake_actions
    )
    response = agent.agent.invoke(
        {"messages": [_Human("旧问题"), _AI("旧的回答"), _Human("新问题")]},
        config={"configurable": {"thread_id": "t-history-2"}},
    )

    assert _last_ai_content(response) == "本轮的新回答"


def test_pending_eviction_deletes_evicted_thread(monkeypatch, fake_actions):
    """回归：待审批队列满而淘汰最旧一条时，必须连同它的 checkpoint 线程一起删。

    否则 InMemorySaver 会随中断次数无限增长（线程泄漏）；被淘汰那条已不可能被续跑，
    留着只有害处。`ApprovalStore.add()` 的返回值就是为调用方清理而设计的。
    """
    approvals = ApprovalStore(maxlen=1)
    agent = _build_assistant(
        monkeypatch,
        [_tool_call("restart_service", {"service": "nginx"})],
        fake_actions=fake_actions,
        approvals=approvals,
    )
    deleted: list[str] = []
    monkeypatch.setattr(agent, "_delete_thread", lambda tid: deleted.append(tid))

    agent.reply("重启 nginx")
    first_thread = approvals.list()[0]["thread_id"]
    agent.reply("再重启一次")  # 容量 1 → 淘汰第一条

    assert first_thread in deleted, "被淘汰的待审批项其线程未清理（泄漏）"
    assert len(approvals.list()) == 1, "容量上限仍应生效（新的那条在）"
