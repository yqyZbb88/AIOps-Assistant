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
from src.interfaces.approval import ApprovalError  # noqa: E402
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


def _build_assistant(monkeypatch, responses, *, fake_actions, diag=None, approvals=None):
    """构建挂了审批中间件的 OpsAssistant（模型被替换为脚本化假模型）。"""
    from src.agent import assistant as assistant_mod

    model = ScriptedChatModel(responses=responses)
    monkeypatch.setattr(assistant_mod, "get_model", lambda: model)
    return OpsAssistant(
        monitor=_monitor(),
        diag=diag,
        agent_actions=fake_actions,
        approvals=approvals if approvals is not None else ApprovalStore(),
    )


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

    def fake_exec(endpoint, command, sudo=False):
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

    # 未启用（出厂默认）→ 不带写能力说明
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
