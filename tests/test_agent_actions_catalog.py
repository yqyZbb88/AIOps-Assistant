"""config/agent_actions.yaml 解析与审批策略校验（纯，离线）。"""
from __future__ import annotations

import pytest

from src.plugins.agent_actions.catalog import (
    DEFAULT_APPROVAL_MAX_PENDING,
    DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    RESERVED_AGENT_TOOL_NAMES,
    AgentActionsConfigError,
    colliding_keys,
    load_agent_actions_config,
    parse_agent_actions,
)

SAMPLE = """
enabled: true
target:
  host: 10.0.0.9
  port: 2222
  username: ops
  password: "placeholder-secret"
output_limit: 1234
approval:
  timeout_seconds: 60
  max_pending: 5
commands:
  restart_service:
    summary: 重启服务
    template: "systemctl restart {service}"
    sudo: true
    params:
      service: { type: str, pattern: "^[a-z]+$", description: 服务名 }
  cleanup_disk:
    summary: 清理磁盘
    template: "cd /opt/ops && bash cleanup_disk.sh"
    sudo: false
    params: {}
"""


def _cmd(config, key):
    return next(c for c in config.commands if c.key == key)


# ---------------------------------------------------------------------------
# 正常解析
# ---------------------------------------------------------------------------
def test_parse_full_sample():
    cfg = parse_agent_actions(SAMPLE)
    assert cfg.enabled is True
    assert cfg.target.host == "10.0.0.9"
    assert cfg.target.port == 2222
    assert cfg.target.username == "ops"
    assert cfg.output_limit == 1234
    assert cfg.approval.timeout_seconds == 60
    assert cfg.approval.max_pending == 5
    assert [c.key for c in cfg.commands] == ["restart_service", "cleanup_disk"]
    assert _cmd(cfg, "restart_service").sudo is True
    assert _cmd(cfg, "cleanup_disk").sudo is False
    assert [p.name for p in _cmd(cfg, "restart_service").params] == ["service"]


def test_approval_defaults_when_section_absent():
    cfg = parse_agent_actions(SAMPLE.replace("approval:\n  timeout_seconds: 60\n  max_pending: 5\n", ""))
    assert cfg.approval.timeout_seconds == DEFAULT_APPROVAL_TIMEOUT_SECONDS
    assert cfg.approval.max_pending == DEFAULT_APPROVAL_MAX_PENDING


# ---------------------------------------------------------------------------
# 审批不可关闭：approval 段只允许两个键
# ---------------------------------------------------------------------------
def test_approval_rejects_unknown_key():
    """写任何多余键（尤其 enabled）都必须报错——配置层不存在关掉审批的开关。"""
    bad = SAMPLE.replace("  max_pending: 5", "  max_pending: 5\n  enabled: false")
    with pytest.raises(AgentActionsConfigError, match="不支持键"):
        parse_agent_actions(bad)


def test_approval_rejects_non_positive_values():
    for field in ("timeout_seconds", "max_pending"):
        value = "60" if field == "timeout_seconds" else "5"
        bad = SAMPLE.replace(f"{field}: {value}", f"{field}: 0")
        with pytest.raises(AgentActionsConfigError, match="不能小于 1"):
            parse_agent_actions(bad)


# ---------------------------------------------------------------------------
# 命令级校验（委托 common/cmddef，这里确认异常被翻译成 AgentActionsConfigError）
# ---------------------------------------------------------------------------
def test_template_placeholder_must_be_declared():
    bad = SAMPLE.replace('"systemctl restart {service}"', '"systemctl restart {oops}"')
    with pytest.raises(AgentActionsConfigError, match="未声明"):
        parse_agent_actions(bad)


def test_declared_but_unused_param_rejected():
    bad = SAMPLE.replace(
        "      service: { type: str, pattern: \"^[a-z]+$\", description: 服务名 }",
        "      service: { type: str, pattern: \"^[a-z]+$\" }\n      unused: { type: int }",
    )
    with pytest.raises(AgentActionsConfigError, match="未使用"):
        parse_agent_actions(bad)


def test_str_param_requires_pattern():
    bad = SAMPLE.replace(', pattern: "^[a-z]+$"', "")
    with pytest.raises(AgentActionsConfigError, match="pattern"):
        parse_agent_actions(bad)


def test_bad_command_key_rejected():
    bad = SAMPLE.replace("  restart_service:", "  Restart-Service:")
    with pytest.raises(AgentActionsConfigError, match="不合法"):
        parse_agent_actions(bad)


def test_empty_host_rejected():
    bad = SAMPLE.replace("host: 10.0.0.9", 'host: ""')
    with pytest.raises(AgentActionsConfigError, match="host"):
        parse_agent_actions(bad)


# ---------------------------------------------------------------------------
# 加载：关闭 / 缺失 / 非法一律 None
# ---------------------------------------------------------------------------
def test_repo_default_config_is_disabled():
    """出厂必须关闭（enabled: false）——写命令能力不能默认可用。"""
    assert load_agent_actions_config() is None


def test_load_missing_file_returns_none(tmp_path):
    assert load_agent_actions_config(tmp_path / "not_here.yaml") is None


def test_load_disabled_returns_none(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text(SAMPLE.replace("enabled: true", "enabled: false"), encoding="utf-8")
    assert load_agent_actions_config(f) is None


def test_load_invalid_returns_none(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text(SAMPLE.replace("  max_pending: 5", "  max_pending: 0"), encoding="utf-8")
    assert load_agent_actions_config(f) is None


def test_load_valid_returns_config(tmp_path):
    f = tmp_path / "a.yaml"
    f.write_text(SAMPLE, encoding="utf-8")
    cfg = load_agent_actions_config(f)
    assert cfg is not None
    assert cfg.target.host == "10.0.0.9"


# ---------------------------------------------------------------------------
# 工具名冲突检测（纯函数）
# ---------------------------------------------------------------------------
def test_colliding_keys():
    assert colliding_keys(["a", "b"], ["b", "c"]) == frozenset({"b"})
    assert colliding_keys(["a"], ["b"]) == frozenset()
    assert colliding_keys([], RESERVED_AGENT_TOOL_NAMES) == frozenset()


def test_reserved_names_are_monitor_tool_names():
    """保留名必须与监控工具集一致——不一致则冲突检测会漏。"""
    from tests.conftest import FakeMonitor
    from src.agent.tools import build_tools

    names = {t.name for t in build_tools(FakeMonitor())}
    assert names == set(RESERVED_AGENT_TOOL_NAMES)
