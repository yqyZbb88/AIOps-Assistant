"""写工具构建器：工具与审批策略成对产出、凭据不进 schema（离线）。"""
from __future__ import annotations

from src.agent.tools import build_agent_action_tools, build_tools
from src.interfaces.command import CommandSpec, ParamSpec
from tests.conftest import FakeAgentActions, FakeMonitor

CONNECTION_FIELDS = ("host", "port", "username", "password", "key_filename", "connect_timeout")


def test_source_none_yields_empty_bundle():
    bundle = build_agent_action_tools(None)
    assert bundle.tools == []
    assert bundle.interrupt_on == {}


def test_tools_and_interrupt_on_are_paired():
    """工具与审批策略成对产出：有工具就必有审批策略（结构上无法只取其一）。"""
    src = FakeAgentActions()
    bundle = build_agent_action_tools(src)

    assert [t.name for t in bundle.tools] == ["restart_service"]
    assert set(bundle.interrupt_on) == {t.name for t in bundle.tools}


def test_interrupt_on_never_allows_edit():
    """edit 会绕过管理员声明的模板与参数约束，必须永不出现。"""
    bundle = build_agent_action_tools(FakeAgentActions())
    for tool_name, policy in bundle.interrupt_on.items():
        assert policy["allowed_decisions"] == ["approve", "reject"], tool_name
        assert "edit" not in policy["allowed_decisions"]


def test_args_schema_has_no_connection_fields():
    """凭据绝不进模型上下文：写工具 schema 只能有语义参数。"""
    bundle = build_agent_action_tools(FakeAgentActions())
    for tool in bundle.tools:
        assert not (set(tool.args) & set(CONNECTION_FIELDS)), tool.name


def test_write_tool_description_mentions_approval():
    bundle = build_agent_action_tools(FakeAgentActions())
    desc = bundle.tools[0].description
    assert "审批" in desc
    assert "改动" in desc


def test_reserved_keys_are_dropped():
    """与已占用工具名冲突的命令被丢弃（重名工具会被静默遮蔽）。"""
    src = FakeAgentActions(
        specs=(
            CommandSpec(key="restart_service", summary="a"),
            CommandSpec(key="cpu_usage", summary="b"),
        )
    )
    bundle = build_agent_action_tools(src, reserved={"cpu_usage"})

    assert [t.name for t in bundle.tools] == ["restart_service"]
    assert "cpu_usage" not in bundle.interrupt_on


def test_reserved_defaults_to_empty():
    src = FakeAgentActions(specs=(CommandSpec(key="cpu_usage", summary="x"),))
    bundle = build_agent_action_tools(src)
    assert [t.name for t in bundle.tools] == ["cpu_usage"]


def test_params_become_tool_args():
    src = FakeAgentActions(
        specs=(
            CommandSpec(
                key="restart_service",
                summary="重启",
                params=(
                    ParamSpec(name="service", type="str", required=True),
                    ParamSpec(name="lines", type="int", default=10),
                ),
            ),
        )
    )
    bundle = build_agent_action_tools(src)
    assert set(bundle.tools[0].args) == {"service", "lines"}


def test_read_tool_description_unchanged():
    """只读诊断工具的措辞必须逐字不变（有既有测试锁，这里再确认一次）。"""
    from tests.conftest import FakeDiagnostics

    diag = FakeDiagnostics()
    from src.agent.tools import build_diag_tools

    for tool in build_diag_tools(diag):
        assert "只读诊断命令，不会改动系统，凭据无需你提供" in tool.description
        assert "审批" not in tool.description


def test_monitor_tools_unchanged_by_write_support():
    """写工具的存在不得影响监控工具的既有行为。"""
    assert [t.name for t in build_tools(FakeMonitor())] == [
        "cpu_usage", "memory_usage", "disk_usage", "active_alerts",
    ]
