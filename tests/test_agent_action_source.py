"""SshAgentActionSource：写命令执行、失败语义、凭据隔离、审计（monkeypatch，离线）。

与只读诊断的关键差异（本文件重点锁死）：
- 远端**非零退出码就是失败**（抛 AgentActionError），而只读诊断把非零当正常返回；
- 参数里夹带连接字段必须被拒，且**不能触达 SSH**（凭据不可由调用方指定）。
"""
from __future__ import annotations

import logging

import pytest

from src.interfaces.agent_action import AgentActionError
from src.plugins.agent_actions.catalog import parse_agent_actions
from src.plugins.agent_actions.source import SshAgentActionSource

SAMPLE = """
enabled: true
target:
  host: 192.168.1.1
  port: 22
  username: root
  password: "secret-in-test"
output_limit: 40
commands:
  restart_service:
    summary: 重启服务
    template: "systemctl restart {service}"
    sudo: true
    params:
      service: { type: str, pattern: "^[a-z]+$", description: 服务名 }
  cleanup_disk:
    summary: 清理磁盘
    template: "bash /opt/ops/cleanup_disk.sh"
    sudo: false
    params: {}
"""


@pytest.fixture
def source() -> SshAgentActionSource:
    return SshAgentActionSource(parse_agent_actions(SAMPLE))


def _stub(monkeypatch, code=0, out="done", err=""):
    """替换共享内核 exec_command，记录调用参数。"""
    calls = []

    def fake(endpoint, command, sudo=False):
        calls.append({"command": command, "sudo": sudo, "host": endpoint.host,
                      "password": endpoint.password})
        return code, out, err

    monkeypatch.setattr("src.plugins.common.ssh.exec_command", fake)
    return calls


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------
def test_run_ok_builds_command_and_passes_sudo(source, monkeypatch):
    calls = _stub(monkeypatch, out="ok")
    result = source.run("restart_service", params={"service": "nginx"})

    assert result["ok"] is True
    assert result["host"] == "192.168.1.1"
    assert result["output"] == "ok"
    assert calls[0]["command"] == "systemctl restart nginx"
    assert calls[0]["sudo"] is True, "sudo 必须按目录声明传递"
    assert calls[0]["password"] == "secret-in-test", "凭据来自配置，不进工具参数"


def test_sudo_false_per_catalog(source, monkeypatch):
    calls = _stub(monkeypatch)
    source.run("cleanup_disk")
    assert calls[0]["sudo"] is False
    assert calls[0]["command"] == "bash /opt/ops/cleanup_disk.sh"


# ---------------------------------------------------------------------------
# 写语义：非零退出码 = 失败
# ---------------------------------------------------------------------------
def test_nonzero_exit_raises(source, monkeypatch):
    """写命令非零退出码就是失败（与只读诊断相反）。"""
    _stub(monkeypatch, code=3, out="", err="Unit nginx.service not found")
    with pytest.raises(AgentActionError, match="exit_code=3"):
        source.run("restart_service", params={"service": "nginx"})


def test_ssh_error_raises_and_audits(source, monkeypatch, caplog):
    from src.plugins.common.ssh import SshError

    def boom(endpoint, command, sudo=False):
        raise SshError("authentication failed")

    monkeypatch.setattr("src.plugins.common.ssh.exec_command", boom)
    with caplog.at_level(logging.WARNING, logger="aiops_assistant.audit"):
        with pytest.raises(AgentActionError, match="SSH"):
            source.run("restart_service", params={"service": "nginx"})
    assert "authentication failed" in caplog.text


# ---------------------------------------------------------------------------
# 凭据隔离 / 命令注入
# ---------------------------------------------------------------------------
def test_connection_fields_rejected_without_reaching_ssh(source, monkeypatch):
    """params 里夹带 host/密码等连接字段必须被拒，且根本不触达 SSH。"""
    calls = _stub(monkeypatch)
    for field in ("host", "password", "port", "username", "key_filename"):
        with pytest.raises(AgentActionError, match="不接受连接参数"):
            source.run("restart_service", params={"service": "nginx", field: "x"})
    assert calls == [], "非法参数绝不能触达 SSH"


def test_unknown_command_rejected(source):
    with pytest.raises(AgentActionError, match="未知写命令"):
        source.run("rm_rf")


def test_injection_param_rejected(source, monkeypatch):
    """不满足 pattern 的参数直接拒绝（未到 shlex 转义那步）。"""
    calls = _stub(monkeypatch)
    with pytest.raises(AgentActionError, match="非法字符"):
        source.run("restart_service", params={"service": "nginx; rm -rf /"})
    assert calls == []


def test_output_truncated(source, monkeypatch):
    _stub(monkeypatch, out="x" * 200)
    result = source.run("cleanup_disk")
    assert "已截断" in result["output"]
    assert len(result["output"]) < 200


# ---------------------------------------------------------------------------
# 审计
# ---------------------------------------------------------------------------
def test_audit_has_no_password_and_no_full_output(source, monkeypatch, caplog):
    _stub(monkeypatch, out="sensitive-output-payload")
    with caplog.at_level(logging.INFO, logger="aiops_assistant.audit"):
        source.run("restart_service", params={"service": "nginx"})

    assert '"action": "agent_action"' in caplog.text
    assert '"command": "restart_service"' in caplog.text
    assert "secret-in-test" not in caplog.text
    assert '"password"' not in caplog.text
    assert "sensitive-output-payload" not in caplog.text, "整段输出不得落审计"


def test_audit_on_nonzero_records_error(source, monkeypatch, caplog):
    _stub(monkeypatch, code=1, err="boom")
    with caplog.at_level(logging.ERROR, logger="aiops_assistant.audit"):
        with pytest.raises(AgentActionError):
            source.run("cleanup_disk")
    assert "rc=1" in caplog.text


# ---------------------------------------------------------------------------
# 构造与清单
# ---------------------------------------------------------------------------
def test_commands_expose_specs(source):
    specs = {s.key: s for s in source.commands()}
    assert set(specs) == {"restart_service", "cleanup_disk"}
    assert [p.name for p in specs["restart_service"].params] == ["service"]


def test_constructor_requires_host():
    cfg = parse_agent_actions(SAMPLE.replace("host: 192.168.1.1", 'host: "x"'))
    # 用一份 host 非空但直接传 None 的配置来验证护栏
    with pytest.raises(AgentActionError, match="target.host"):
        SshAgentActionSource(None)  # type: ignore[arg-type]
    assert cfg.target.host == "x"  # 有效配置不受影响
