"""审批到 SSH 退出状态、持久审计和模型工具反馈的离线回归。"""
from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from src.agent.assistant import OpsAssistant
from src.interfaces.action import ActionError, ActionOutcomeUnknown
from src.interfaces.approval import ApprovalError
from src.interfaces.host import HostSshTarget
from src.plugins.actions.catalog import parse_actions
from src.plugins.actions.executor import CatalogCommandExecutor
from src.plugins.agent_actions.catalog import parse_agent_actions
from src.plugins.agent_actions.source import SshAgentActionSource
from src.plugins.common.ssh import SshOutcomeUnknown, _SUDO_LAUNCHER, exec_command
from src.plugins.common.endpoint import SshEndpoint
from src.storage.approval import ApprovalStore
from src.storage.db import open_database
from src.storage.execution import SqliteActionExecutionStore
from tests.conftest import FakeMonitor
from tests.test_agent_actions_hitl_graph import ScriptedChatModel, _tool_call
from tests.test_ssh_security import FakeChannel, FakeClient, FakeRejectPolicy
from tests.test_rule_claim_regression import setup_engine
from tests.ssh_script_cases import compatibility_cases


CATALOG = """
enabled: true
commands:
  kill_bash:
    summary: 停止合成负载
    template: 'pkill -9 -f "load_gen.sh"'
    sudo: true
    params: {}
"""


def install_client(monkeypatch, *, code=0, failure=""):
    """替换 Paramiko 网络边界，仍运行公共执行器和状态读取器。"""
    calls = []
    channel = FakeChannel()
    channel.recv_exit_status = lambda: code
    channel.write_closed = False

    def shutdown_write():
        if failure == "shutdown_write":
            raise OSError("synthetic EOF failure")
        channel.write_closed = True

    channel.shutdown_write = shutdown_write

    class Input(io.BytesIO):
        def write(self, data):
            if failure == "write":
                raise OSError("synthetic write failure")
            return super().write(data)

        def flush(self):
            if failure == "flush":
                raise OSError("synthetic flush failure")
            return super().flush()

    stdin = Input()
    stdin.channel = channel

    class Client(FakeClient):
        def connect(self, **kwargs):
            self.connection = kwargs

        def exec_command(self, command, **kwargs):
            calls.append(command)
            return stdin, SimpleNamespace(channel=channel), None

    client = Client()
    monkeypatch.setitem(sys.modules, "paramiko", SimpleNamespace(
        SSHClient=lambda: client, RejectPolicy=FakeRejectPolicy,
    ))
    return client, stdin, channel, calls


@pytest.mark.parametrize("command", [
    'pkill -9 -f "load_gen.sh"',
    "printf '%s\\n' '含引号 \" 与中文'\nexit 3",
])
def test_sudo_script_is_sent_unchanged_through_stdin(monkeypatch, command):
    client, stdin, channel, calls = install_client(monkeypatch)
    result = exec_command(SshEndpoint(host="synthetic", known_hosts="trusted-keys"), command, sudo=True)
    assert calls == [_SUDO_LAUNCHER]
    assert "load_gen.sh" not in calls[0]
    assert stdin.getvalue() == (command + "\n").encode("utf-8")
    assert channel.write_closed and client.closed
    assert result.exit_code == 0 and result.stdout == result.stderr == ""
    assert client.loaded_paths == ["trusted-keys"]
    assert isinstance(client.policy, FakeRejectPolicy)


def test_non_sudo_keeps_existing_shell_command(monkeypatch):
    _, stdin, channel, calls = install_client(monkeypatch)
    exec_command(SshEndpoint(host="synthetic"), "printf ok", sudo=False)
    assert calls == ["printf ok"] and stdin.getvalue() == b""
    assert not channel.write_closed


@pytest.mark.parametrize("failure", ["write", "flush", "shutdown_write"])
def test_script_send_failure_is_unknown_without_retry(monkeypatch, failure):
    client, _, _, calls = install_client(monkeypatch, failure=failure)
    with pytest.raises(SshOutcomeUnknown):
        exec_command(SshEndpoint(host="synthetic"), "true", sudo=True)
    assert calls == [_SUDO_LAUNCHER] and client.closed


@pytest.mark.skipif(not shutil.which("sh"), reason="需要 POSIX sh，标准 test 镜像提供")
@pytest.mark.parametrize("name,script", compatibility_cases().items())
def test_sudo_loader_matches_noninteractive_sh_c(name, script):
    # 仅去掉 sudo 提权层，其余使用实际固定加载器；业务命令不应读到传输中的脚本。
    launcher = shlex.split(_SUDO_LAUNCHER)
    assert launcher[:3] == ["sudo", "sh", "-c"]
    original = subprocess.run(["sh", "-c", script], input=b"", capture_output=True, timeout=5)
    current = subprocess.run(launcher[1:], input=(script + "\n").encode(), capture_output=True, timeout=5)
    assert (current.returncode, current.stdout) == (original.returncode, original.stdout)
    if name in {"syntax_error", "missing_command"}:
        assert current.stderr and original.stderr  # 诊断前缀允许包含 eval 上下文。
    else:
        assert current.stderr == original.stderr


@pytest.mark.skipif(not shutil.which("sh"), reason="需要 POSIX sh，标准 test 镜像提供")
def test_sudo_loader_refuses_partial_script_when_read_fails(tmp_path):
    cat = tmp_path / "cat"
    cat.write_text("#!/bin/sh\nprintf 'printf UNEXPECTED_EXECUTION'\nexit 23\n")
    cat.chmod(0o700)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}")
    current = subprocess.run(shlex.split(_SUDO_LAUNCHER)[1:], input=b"printf intended\n",
                             env=env, capture_output=True, timeout=5)
    assert current.returncode == 23 and current.stdout == b""


class CapturingModel(ScriptedChatModel):
    tool_feedback: list[str] = []

    def _generate(self, messages, **kwargs):
        self.tool_feedback.extend(str(m.content) for m in messages if isinstance(m, ToolMessage))
        return super()._generate(messages, **kwargs)


@pytest.mark.parametrize("code,status,feedback", [
    (0, "succeeded", "写命令已执行"),
    (1, "failed", "写命令执行失败"),
    (137, "failed", "写命令执行失败"),
    (-1, "unknown", "写命令执行结果未知"),
])
def test_real_approval_resume_preserves_ssh_outcome(monkeypatch, tmp_path, code, status, feedback):
    _, _, _, calls = install_client(monkeypatch, code=code)
    db = open_database(tmp_path / "outcomes.db")
    store = SqliteActionExecutionStore(db)
    source = SshAgentActionSource(parse_agent_actions(CATALOG), target=HostSshTarget(host="synthetic"))
    source.configure_execution(store, host_id="node-a", target_summary="node-a (A)", config_fingerprint="test")
    model = CapturingModel(responses=[_tool_call("kill_bash", {}), AIMessage(content="工具结果已返回")])
    monkeypatch.setattr("src.agent.assistant.get_model", lambda: model)
    agent = OpsAssistant(monitor=FakeMonitor(), agent_actions=source,
                         approvals=ApprovalStore(), host_id="node-a")
    assert "待人工审批" in agent.reply("停止合成负载")
    assert calls == [] and store.recent() == []
    approval_id = agent.pending_approvals()[0].approval_id
    assert agent.decide(approval_id, action="approve").status == "completed"
    row, = store.recent()
    assert row.status == status and row.approval_id == approval_id
    assert row.host_id == "node-a" and row.detail
    assert any(feedback in message for message in model.tool_feedback)
    if code > 0:
        assert f"exit_code={code}" in row.detail
        assert "(无输出)" in row.detail
    if code == -1:
        assert "未返回退出状态" in row.detail and "exit_code=-1" in row.detail
        assert all("写命令执行失败" not in message for message in model.tool_feedback)
    with pytest.raises(ApprovalError):
        agent.decide(approval_id, action="approve")
    assert calls == [_SUDO_LAUNCHER] and len(store.recent()) == 1


@pytest.mark.parametrize("code,error", [(-1, ActionOutcomeUnknown), (1, ActionError)])
def test_rule_executor_uses_same_exit_semantics(monkeypatch, code, error):
    _, _, _, calls = install_client(monkeypatch, code=code)
    executor = CatalogCommandExecutor(parse_actions(CATALOG).commands[0])
    with pytest.raises(error):
        executor.execute_on(HostSshTarget(host="synthetic"), {})
    assert calls == [_SUDO_LAUNCHER]


@pytest.mark.parametrize("code,status,remediation_status", [
    (0, "succeeded", "ok"), (1, "failed", "failed"), (-1, "unknown", "unknown"),
])
def test_rule_engine_persists_same_ssh_outcome(monkeypatch, tmp_path, code, status, remediation_status):
    _, _, _, calls = install_client(monkeypatch, code=code)
    _, store, _, agent, payload = setup_engine(tmp_path)
    cfg = parse_actions("""
commands:
  restart_service:
    template: 'printf %s {service}'
    sudo: true
    params:
      service: {type: str, pattern: '^[a-z]+$'}
""")
    agent.rule_engine.executors["restart_service"] = CatalogCommandExecutor(cfg.commands[0])
    record, = agent.process_alert(payload).remediation
    execution, = store.recent()
    assert record["status"] == remediation_status and execution.status == status
    assert record["execution_id"] == execution.execution_id
    assert execution.alert_key == payload.alerts[0].alert_key
    assert execution.detail and calls == [_SUDO_LAUNCHER]
    if code == 1:
        assert "rc=1" in execution.detail
    if code == -1:
        assert "未返回退出状态" in execution.detail
    agent.process_alert(payload)
    assert calls == [_SUDO_LAUNCHER] and len(store.recent()) == 1
