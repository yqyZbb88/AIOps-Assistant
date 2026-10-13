"""仅用于独立测试容器的页面验收服务；合成身份/主机/SSH，禁止外部连接。"""
import tempfile
import time
import argparse
from pathlib import Path

import paramiko
import uvicorn
import yaml

import tests.conftest  # noqa: F401 - 在应用导入前启用离线保护
from tests.test_admin_config import _setup
from tests.test_host_access import manager_for
from src.main import create_app
from src.plugins.monitors.prometheus_monitor import PrometheusMonitor
from src.utils.admin_credentials import AdminCredentials
from src.utils.config_loader import load_settings
from src.utils.host_onboarding import HostOnboarding


def app(*, slow_approval=False):
    root = Path(tempfile.mkdtemp(prefix="console-acceptance-"))
    paths, _, _ = _setup(root, target_mode="managed")
    config = yaml.safe_load(paths["settings"].read_text(encoding="utf-8"))
    config["llm"] = {"enabled": False}
    config["storage"] = {"enabled": True, "db_path": str(root / "history.db")}
    config["hosts"] = {"file": str(paths["hosts"])}
    config["logging"] = {"audit": False, "audit_file": "", "inspection_file": ""}
    config["auth"]["host_access"] = '{"alice":["node-a"]}'
    paths["settings"].write_text(yaml.safe_dump(config), encoding="utf-8")
    settings = load_settings(paths["settings"])
    manager = manager_for(paths, settings, root)
    # 不创建 HTTP/SSH 客户端，页面指标来自合成 Prometheus 响应。
    PrometheusMonitor.check_health = lambda self: None
    def synthetic(self, path, params):
        labels = {"job": "node", "instance": "10.0.0.10:9100"}
        if path == "alerts":
            return {"alerts": []}
        if path == "query_range":
            return {"result": [{"metric": labels, "values": [[params["start"], "1"], [params["end"], "1"]]}]}
        return {"result": [{"metric": labels, "value": [1791849600, "1"]}]}
    PrometheusMonitor._request = synthetic
    HostOnboarding._targets = lambda self: [{"job": "node", "instance": "10.0.0.10:9100", "address": "10.0.0.10:9100", "health": "up", "ssh_eligible": True}]
    HostOnboarding.check_registered_ssh = lambda *args: (_ for _ in ()).throw(AssertionError("页面验收禁止 SSH"))
    application = create_app(settings=settings, admin_config_manager=manager)
    key = paramiko.RSAKey.generate(1024)
    known = "10.0.0.10 ssh-rsa " + key.get_base64() + "\n"
    AdminCredentials(manager).change(manager.revision(), "ssh", "editable", "save", {
        "username": "old-user", "method": "password", "password": "synthetic-password", "known_hosts": known}, "admin")
    sessions = application.state.container.sessions()
    session = sessions.create(user="alice", host_id="node-a")
    sessions.record_request({"approval_id": "browser-history", "session_id": session.session_id, "user": "alice",
        "host_id": "node-a", "actions": [{"name": "restart", "args": {"service": "demo"}}], "created_at": "2026-10-13T00:00:00+00:00"})
    sessions.record_decision("browser-history", session_id=session.session_id, action="approve", decided_by="admin", host_id="node-a")
    application.state.container.execution_store().create(channel="agent", idempotency_key="browser-history", host_id="node-a",
        action_name="restart", params={}, approval_id="browser-history", status="failed")
    if slow_approval:
        from src.interfaces.approval import ApprovalError, ApprovalOutcome, PendingApproval
        pending_session = sessions.create(user="alice", host_id="node-a")
        approval_id = "browser-slow-approval"
        sessions.record_request({"approval_id": approval_id, "session_id": pending_session.session_id, "user": "alice",
            "host_id": "node-a", "actions": [{"name": "synthetic_action", "args": {}}], "created_at": "2026-10-13T00:00:00+00:00"})
        sessions.append_turn(pending_session.session_id, user_content="synthetic request", assistant_content="pending",
                             status="pending_approval", approval_id=approval_id)
        class SlowApproval:
            pending = True
            def pending_approvals(self):
                return [PendingApproval(approval_id=approval_id, created_at="2026-10-13T00:00:00+00:00", user="alice",
                    host_id="node-a", session_id=pending_session.session_id, actions=({"name": "synthetic_action"},))] if self.pending else []
            def decide(self, current_id, *, action, reason=""):
                return self.decide_as(current_id, action=action, reason=reason, decided_by="admin")
            def decide_as(self, current_id, *, action, reason="", decided_by=None):
                if current_id != approval_id or not self.pending:
                    raise ApprovalError("synthetic approval already consumed")
                self.pending = False
                time.sleep(1)  # 模拟服务端尚未确认，不得提前显示审批通过。
                sessions.record_decision(approval_id, session_id=pending_session.session_id, action=action, decided_by=decided_by, host_id="node-a")
                if action == "approve":
                    executions = application.state.container.execution_store()
                    execution = executions.create(channel="agent", idempotency_key=approval_id, host_id="node-a",
                        action_name="synthetic_action", params={}, approval_id=approval_id, status="running")
                    time.sleep(3)
                    executions.finish(execution.execution_id, status="succeeded")
                time.sleep(6)  # 只模拟 AI 延迟，不创建模型或执行任何外部命令。
                return ApprovalOutcome(approval_id, action, "completed", "合成 AI 反馈：已核对执行记录")
        application.state.agent.chat_service._assistant = SlowApproval()
    return application


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--slow-approval", action="store_true")
    uvicorn.run(app(slow_approval=parser.parse_args().slow_approval), host="0.0.0.0", port=8000, log_level="warning")
