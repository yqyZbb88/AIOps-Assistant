from src.core.chat import ChatService
from src.core.agent import OpsAgent
from src.core.rule_engine import (
    HOST_UNAUTHORIZED_MARK,
    HOST_UNRESOLVED_MARK,
    RuleEngine,
)
from src.interfaces.action import ActionExecutor
from src.interfaces.metric import MetricSample
from src.interfaces.host import HostSshTarget
from src.interfaces.agent_action import AgentActionError
from src.interfaces.diagnostic import DiagnosticError
from src.plugins.agent_actions.catalog import parse_agent_actions
from src.plugins.agent_actions.source import SshAgentActionSource
from src.plugins.diagnostics.commands import parse_diagnostics
from src.plugins.diagnostics.ssh_diagnostics import SshDiagnostics
from src.storage.db import open_database
from src.storage.execution import SqliteActionExecutionStore
from src.storage.history import SqliteHistoryStore
from src.storage.session import InMemorySessionStore
from src.utils.hosts_config import parse_hosts

from tests.conftest import FakeMonitor, make_alert, make_payload


HOSTS = """
version: 1
credentials:
  a: {username: ops, password: secret-a}
  b: {username: ops, password: secret-b}
hosts:
  - id: host-a
    name: Host A
    enabled: true
    prometheus: {job: node-a, instance: "a:9100"}
    ssh: {host: "ssh-a", credential_ref: a}
    allow:
      diagnostics: [memory]
      rule_actions: [restart_service]
      agent_actions: [restart_service]
  - id: host-b
    name: Host B
    enabled: true
    prometheus: {job: node-b, instance: "b:9100"}
    ssh: {host: "ssh-b", credential_ref: b}
    allow:
      diagnostics: [disk]
      rule_actions: []
      agent_actions: []
"""


class TargetExecutor(ActionExecutor):
    name = "restart_service"

    def __init__(self):
        self.calls = []

    def execute(self, params):
        raise AssertionError("多主机规则不得回退到 params/default SSH 目标")

    def execute_on(self, target, params):
        self.calls.append((target.host, dict(params)))
        return {"ok": True, "host": target.host}


def _rules(tmp_path, target="a:9100"):
    path = tmp_path / "rules.yaml"
    path.write_text(
        f"""
rules:
  - alertname: HighCPU
    action: restart_service
    target_instance: "{target}"
    params:
      host: attacker-controlled
      password: should-be-ignored
      service: nginx
    auto_execute: true
""",
        encoding="utf-8",
    )
    return path


def test_rule_execution_uses_resolved_host_target_only(tmp_path):
    hosts = parse_hosts(HOSTS)
    executor = TargetExecutor()
    engine = RuleEngine(
        executors={"restart_service": executor},
        rules_file=_rules(tmp_path),
        hosts=hosts,
    )

    record = engine.execute_for_payload(
        make_payload(make_alert("HighCPU", instance="a:9100"))
    )[0]

    assert record["status"] == "ok"
    assert record["host_id"] == "host-a"
    assert executor.calls == [("ssh-a", {"service": "nginx"})]


def test_unknown_or_unauthorized_host_never_executes(tmp_path):
    hosts = parse_hosts(HOSTS)
    executor = TargetExecutor()
    unknown_engine = RuleEngine(
        executors={"restart_service": executor},
        rules_file=_rules(tmp_path),
        hosts=hosts,
    )
    unknown = unknown_engine.execute_for_payload(
        make_payload(make_alert("HighCPU", instance="outside:9100"))
    )[0]

    denied_engine = RuleEngine(
        executors={"restart_service": executor},
        rules_file=_rules(tmp_path, target="b:9100"),
        hosts=hosts,
    )
    denied = denied_engine.execute_for_payload(
        make_payload(make_alert("HighCPU", instance="b:9100"))
    )[0]

    assert unknown["status"] == HOST_UNRESOLVED_MARK
    assert denied["status"] == HOST_UNAUTHORIZED_MARK
    assert executor.calls == []


def test_chat_monitor_fallback_is_bound_to_session_host():
    hosts = parse_hosts(HOSTS)

    class TargetMonitor(FakeMonitor):
        def __init__(self):
            super().__init__()
            self.targets = []

        def query_metric(self, metric_id, *, selector=None, target=None):
            self.targets.append(target)
            value = 11.0 if target.instance == "a:9100" else 22.0
            return [MetricSample(metric_id, {"instance": target.instance}, value)]

    monitor = TargetMonitor()
    sessions = InMemorySessionStore()
    a = sessions.create(host_id="host-a")
    b = sessions.create(host_id="host-b")
    service = ChatService(monitor, sessions=sessions, hosts=hosts)

    assert "11.0%" in service.reply_turn("CPU", session_id=a.session_id).reply
    assert "22.0%" in service.reply_turn("CPU", session_id=b.session_id).reply
    assert [(t.job, t.instance) for t in monitor.targets] == [
        ("node-a", "a:9100"),
        ("node-b", "b:9100"),
    ]


def test_diagnostics_and_agent_action_use_bound_host_only(tmp_path, monkeypatch):
    calls = []

    def fake_exec(endpoint, command, sudo=False, output_limit=4000):
        calls.append((endpoint.host, endpoint.password, command, sudo))
        return 0, "ok", ""

    monkeypatch.setattr("src.plugins.common.ssh.exec_command", fake_exec)
    target = HostSshTarget(
        host="ssh-a", username="ops", password="secret-a", connect_timeout=8
    )
    diag_cfg = parse_diagnostics("""
enabled: true
provider: ssh
commands:
  memory: {summary: memory, template: "free -m", params: {}}
  disk: {summary: disk, template: "df -h", params: {}}
""")
    diag = SshDiagnostics(
        diag_cfg, target=target, allowed_commands=frozenset({"memory"})
    )
    assert diag.run("memory")["host"] == "ssh-a"
    try:
        diag.run("disk")
    except DiagnosticError:
        pass
    else:
        raise AssertionError("未授权诊断命令必须被拒绝")

    action_cfg = parse_agent_actions("""
enabled: true
commands:
  restart_service:
    summary: restart
    template: "systemctl restart {service}"
    sudo: true
    params:
      service: {type: str, pattern: "^[a-z]+$"}
""")
    source = SshAgentActionSource(
        action_cfg,
        target=target,
        allowed_commands=frozenset({"restart_service"}),
    )
    store = SqliteActionExecutionStore(open_database(tmp_path / "ops.db"))
    source.configure_execution(
        store,
        host_id="host-a",
        target_summary="host-a (Host A)",
        config_fingerprint="cfg-a",
    )
    try:
        source.run("restart_service", params={"service": "nginx"})
    except AgentActionError:
        pass
    else:
        raise AssertionError("缺少审批上下文的写命令必须被拒绝")

    with source.approval_context("ap-1"):
        result = source.run("restart_service", params={"service": "nginx"})

    assert result["host"] == "ssh-a"
    assert calls == [
        ("ssh-a", "secret-a", "free -m", False),
        ("ssh-a", "secret-a", "systemctl restart nginx", True),
    ]
    execution = store.recent(host_id="host-a")[0]
    assert execution.approval_id == "ap-1"
    assert execution.status == "succeeded"


def test_dual_host_rule_alert_history_execution_closed_loop(tmp_path):
    hosts = parse_hosts(HOSTS)
    db = open_database(tmp_path / "ops.db")
    executions = SqliteActionExecutionStore(db)
    history = SqliteHistoryStore(
        db,
        ttl_seconds=3600,
        max_rows=100,
        purge_interval_seconds=float("inf"),
    )
    executor = TargetExecutor()
    rules = RuleEngine(
        executors={"restart_service": executor},
        rules_file=_rules(tmp_path),
        hosts=hosts,
        execution_store=executions,
    )
    agent = OpsAgent(
        FakeMonitor(),
        rule_engine=rules,
        history=history,
        hosts=hosts,
    )

    ack = agent.process_alert(
        make_payload(make_alert("HighCPU", instance="a:9100"))
    )

    assert ack.alerts[0]["host_id"] == "host-a"
    assert ack.remediation[0]["host_id"] == "host-a"
    assert ack.remediation[0]["execution_id"]
    assert history.recent_alerts(host_id="host-a")[0].host_id == "host-a"
    archived = history.recent_remediations(host_id="host-a")[0]
    assert archived.execution_id == ack.remediation[0]["execution_id"]
    assert history.recent_alerts(host_id="host-b") == []
    execution = executions.recent(host_id="host-a")[0]
    assert execution.status == "succeeded"
    assert execution.alert_key == ack.remediation[0]["alert_key"]
    assert executor.calls == [("ssh-a", {"service": "nginx"})]
