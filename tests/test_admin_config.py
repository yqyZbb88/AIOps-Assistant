"""管理员配置 API 与 round-trip 写回（全部使用临时文件，不接触真实主机）。"""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from src.main import create_app
from src.utils.admin_config import AdminConfigManager, AdminConfigUnavailable
from src.utils.config_loader import AgentSettings, AuthSettings, HostsSettings, LLMSettings, Settings
from src.utils.config_loader import MetricsSettings, PrometheusSettings


def _files(tmp_path):
    paths = {name: tmp_path / f"{name}.yaml" for name in (
        "settings", "hosts", "rules", "actions", "agent_actions", "diagnostics"
    )}
    paths["settings"].write_text(
        """# keep-settings-comment
llm:
  enabled: true
agent:
  enabled: false
inspection:
  enabled: false
auth:
  enabled: true
  tokens: '{"admin-token":"admin","user-token":"alice","alert-token":"alertmanager"}'
  approval_mode: approvers_only
  approver_identities: '["admin"]'
  admin_identities: '["admin"]'
  alert_identity: alertmanager
""", encoding="utf-8"
    )
    paths["hosts"].write_text(
        """version: 1
credentials:
  secret:
    username: root
    password: super-secret
    known_hosts: /secret/known_hosts
hosts:
  - id: node-a
    name: 节点 A
    enabled: true
    prometheus:
      job: node
      instance: 10.0.0.10:9100
    ssh:
      host: 10.0.0.10
      port: 22
      credential_ref: secret
    allow:
      diagnostics: []
      rule_actions: [restart_service]
      agent_actions: []
""", encoding="utf-8"
    )
    paths["rules"].write_text(
        """# keep-rules-comment
auto_execute_enabled: false
rules:
  - alertname: HighCPUUsage
    action: restart_service
    target_instance: 10.0.0.10:9100
    params:
      host: 10.0.0.10
      password: super-secret
      service: nginx
    auto_execute: false
    description: test
""", encoding="utf-8"
    )
    paths["actions"].write_text(
        """enabled: true
commands:
  restart_service:
    enabled: true
    summary: restart
    template: "systemctl restart {service}"
    sudo: true
    params:
      service:
        type: str
        pattern: "^[A-Za-z0-9_.:@-]{1,128}$"
""", encoding="utf-8"
    )
    paths["agent_actions"].write_text("enabled: false\ncommands: {}\n", encoding="utf-8")
    paths["diagnostics"].write_text("enabled: false\ncommands: {}\n", encoding="utf-8")
    return paths


def _setup(tmp_path, *, auth_enabled=True, target_mode="external"):
    paths = _files(tmp_path)
    target_file = tmp_path / "targets" / "assistant.yml"
    if target_mode == "managed":
        with paths["settings"].open("a", encoding="utf-8") as handle:
            handle.write(
                "prometheus:\n  target_mode: managed\n  file_sd_path: '"
                + target_file.as_posix() + "'\n"
            )
    auth = AuthSettings(
        enabled=auth_enabled,
        tokens='{"admin-token":"admin","user-token":"alice","alert-token":"alertmanager"}',
        approval_mode="approvers_only",
        approver_identities='["admin"]',
        admin_identities='["admin"]',
        alert_identity="alertmanager",
    )
    settings = Settings(
        llm=LLMSettings(enabled=False),
        agent=AgentSettings(enabled=False),
        hosts=HostsSettings(file=str(paths["hosts"])),
        prometheus=PrometheusSettings(target_mode=target_mode,
                                      file_sd_path=target_file.as_posix() if target_mode == "managed" else ""),
        auth=auth,
    )
    manager = AdminConfigManager(
        settings,
        settings_file=paths["settings"], hosts_file=paths["hosts"],
        rules_file=paths["rules"], actions_file=paths["actions"],
        agent_actions_file=paths["agent_actions"], diagnostics_file=paths["diagnostics"],
        backup_root=tmp_path / "backups",
    )
    return paths, settings, manager


def test_managed_host_add_is_hot_applied_idempotent_and_keeps_write_auth_empty(tmp_path):
    paths, settings, manager = _setup(tmp_path, target_mode="managed")
    draft = {"host_id": "node-c", "name": "节点 C", "exporter_address": "10.0.0.30:9100",
             "credential_id": "", "diagnostics": []}
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        assert client.get("/admin/hosts/options").status_code == 401
        assert client.get("/admin/hosts/options", headers=_auth("user-token")).status_code == 403
        options = client.get("/admin/hosts/options", headers=_auth("admin-token"))
        assert options.status_code == 200, options.text
        assert options.json()["mode"] == "managed"
        assert "super-secret" not in options.text and "known_hosts" not in options.text
        revision = options.json()["revision"]
        preview = client.post("/admin/hosts/preview", headers=_auth("admin-token"),
                              json={"revision": revision, "draft": draft})
        assert preview.status_code == 200, preview.text
        assert preview.json()["exporter_status"] == "down"
        added = client.post("/admin/hosts/add", headers=_auth("admin-token"),
                            json={"revision": revision, "draft": draft})
        assert added.status_code == 200, added.text
        assert added.json()["active_revision"] == added.json()["revision"]
        assert added.json()["message"] == "已登记、Exporter 不可达"
        repeated = client.post("/admin/hosts/add", headers=_auth("admin-token"),
                               json={"revision": revision, "draft": draft})
        assert repeated.status_code == 200 and repeated.json()["existing"] is True
        hosts = client.get("/hosts", headers=_auth("admin-token")).json()
        new = next(item for item in hosts if item["host_id"] == "node-c")
        assert new["capabilities"] == {"diagnostics": [], "rule_actions": [], "agent_actions": []}
        assert new["ssh_configured"] is False
        registry = manager.runtime.current.container.hosts()
        assert registry.resolve_labels({"job": "aiops-hosts", "instance": "10.0.0.30:9100"}).host_id == "node-c"
        assert registry.resolve_labels({"job": "node", "instance": "10.0.0.10:9100"}).host_id == "node-a"
        assert "10.0.0.30:9100" in manager.target_path.read_text(encoding="utf-8")
        assert "node-c" in paths["hosts"].read_text(encoding="utf-8")


def test_external_host_must_select_discovered_target_and_reject_duplicates(tmp_path, monkeypatch):
    from src.utils.host_onboarding import HostOnboarding

    _, settings, manager = _setup(tmp_path)
    monkeypatch.setattr(HostOnboarding, "_targets", lambda self: [
        {"job": "node", "instance": "10.0.0.30:9100", "address": "10.0.0.30:9100",
         "health": "down", "ssh_eligible": True}
    ])
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        revision = client.get("/admin/hosts/options", headers=_auth("admin-token")).json()["revision"]
        draft = {"host_id": "node-c", "name": "节点 C", "job": "node",
                 "instance": "10.0.0.30:9100", "diagnostics": []}
        invalid = client.post("/admin/hosts/preview", headers=_auth("admin-token"),
                              json={"revision": revision, "draft": {**draft, "exporter_address": "10.0.0.40:9100"}})
        assert invalid.status_code == 422
        added = client.post("/admin/hosts/add", headers=_auth("admin-token"),
                            json={"revision": revision, "draft": draft})
        assert added.status_code == 200, added.text
        available_targets = HostOnboarding._targets
        monkeypatch.setattr(HostOnboarding, "_targets",
                            lambda self: (_ for _ in ()).throw(
                                AdminConfigUnavailable("Prometheus 目标查询失败")
                            ))
        repeated = client.post("/admin/hosts/add", headers=_auth("admin-token"),
                               json={"revision": revision, "draft": draft})
        assert repeated.status_code == 200 and repeated.json()["existing"] is True
        monkeypatch.setattr(HostOnboarding, "_targets", available_targets)
        conflict = client.post("/admin/hosts/add", headers=_auth("admin-token"),
                               json={"revision": revision, "draft": {**draft, "host_id": "node-d"}})
        assert conflict.status_code == 409


def test_target_options_only_expose_required_labels_and_block_proxy_ssh(tmp_path, monkeypatch):
    import requests

    _, settings, manager = _setup(tmp_path)

    class FakeResponse:
        def raise_for_status(self):
            pass
        def json(self):
            return {"status": "success", "data": {"activeTargets": [
                {"labels": {"job": "node", "instance": "10.0.0.30:9100", "secret": "hidden-label"},
                 "discoveredLabels": {"__address__": "10.0.0.30:9100", "token": "hidden-label"},
                 "health": "down"},
                {"labels": {"job": "proxy", "instance": "10.0.0.40:9100"},
                 "discoveredLabels": {"__address__": "proxy.internal:9115"},
                 "health": "up"},
            ]}}

    monkeypatch.setattr(requests, "get", lambda *_args, **_kwargs: FakeResponse())
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        options = client.get("/admin/hosts/options", headers=_auth("admin-token"))
        assert options.status_code == 200
        assert "hidden-label" not in options.text
        assert options.json()["targets"][0]["health"] == "down"
        assert options.json()["targets"][1]["ssh_eligible"] is False
        revision = options.json()["revision"]
        proxy = client.post("/admin/hosts/preview", headers=_auth("admin-token"),
                            json={"revision": revision, "draft": {
                                "host_id": "node-c", "name": "代理目标",
                                "job": "proxy", "instance": "10.0.0.40:9100",
                                "credential_id": "secret",
                            }})
        assert proxy.status_code == 422


def test_host_add_rolls_back_host_and_target_if_second_write_fails(tmp_path, monkeypatch):
    paths, settings, manager = _setup(tmp_path, target_mode="managed")
    original_host = paths["hosts"].read_text(encoding="utf-8")
    original_revision = manager.revision()
    real_write = manager._atomic_write

    def fail_target(path, text):
        if path == manager.target_path:
            raise OSError("simulated target failure")
        real_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write", fail_target)
    draft = {"host_id": "node-c", "name": "节点 C", "exporter_address": "10.0.0.30:9100"}
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        response = client.post("/admin/hosts/add", headers=_auth("admin-token"),
                               json={"revision": original_revision, "draft": draft})
    assert response.status_code == 500
    assert paths["hosts"].read_text(encoding="utf-8") == original_host
    assert not manager.target_path.exists()
    assert manager.revision() == original_revision
    assert manager.runtime.current.revision == original_revision


def test_ssh_check_never_runs_command_and_diagnostics_need_verified_ticket(tmp_path, monkeypatch):
    from src.utils import host_onboarding

    paths, settings, manager = _setup(tmp_path, target_mode="managed")
    paths["diagnostics"].write_text(
        "enabled: true\nprovider: ssh\ntarget: {host: localhost}\n"
        "commands:\n  memory: {summary: memory, template: 'free -m', params: {}}\n",
        encoding="utf-8",
    )
    manager.active_revision = manager.revision()  # 模拟应用进程在修改后的配置上启动
    draft = {"host_id": "node-c", "name": "节点 C", "exporter_address": "10.0.0.30:9100",
             "credential_id": "secret", "ssh_port": 22, "diagnostics": ["memory"]}
    calls = []

    class FakeSsh:
        def connect(self, **kwargs):
            calls.append(kwargs)
        def close(self):
            calls.append("closed")
        def exec_command(self, *_args, **_kwargs):
            raise AssertionError("SSH 测试禁止执行命令")

    monkeypatch.setattr(host_onboarding, "_new_client", lambda _module, _endpoint: FakeSsh())
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        revision = manager.revision()
        unverified = client.post("/admin/hosts/add", headers=_auth("admin-token"),
                                 json={"revision": revision, "draft": draft})
        assert unverified.status_code == 422
        checked = client.post("/admin/hosts/ssh-check", headers=_auth("admin-token"),
                              json={"revision": revision, "draft": {**draft, "diagnostics": []}})
        assert checked.status_code == 200, checked.text
        assert checked.json()["verified"] is True
        assert len(calls) == 2 and calls[0]["allow_agent"] is False
        added = client.post("/admin/hosts/add", headers=_auth("admin-token"),
                            json={"revision": revision, "draft": {
                                **draft, "verification_id": checked.json()["verification_id"]
                            }})
        assert added.status_code == 200, added.text
        host = next(h for h in client.get("/hosts", headers=_auth("admin-token")).json()
                    if h["host_id"] == "node-c")
        assert host["capabilities"]["diagnostics"] == ["memory"]
        assert host["capabilities"]["rule_actions"] == []
        assert host["capabilities"]["agent_actions"] == []


def test_ssh_host_key_rejection_is_generic_and_does_not_register_host(tmp_path, monkeypatch):
    from src.utils import host_onboarding

    _, settings, manager = _setup(tmp_path, target_mode="managed")
    monkeypatch.setattr(host_onboarding, "_new_client",
                        lambda _module, _endpoint: (_ for _ in ()).throw(ValueError("secret key path")))
    draft = {"host_id": "node-c", "name": "节点 C", "exporter_address": "10.0.0.30:9100",
             "credential_id": "secret"}
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        response = client.post("/admin/hosts/ssh-check", headers=_auth("admin-token"),
                               json={"revision": manager.revision(), "draft": draft})
    assert response.status_code == 422
    assert "secret key path" not in response.text
    assert "node-c" not in manager.paths["hosts"].read_text(encoding="utf-8")


def test_busy_operations_and_pending_approvals_reject_hot_update(tmp_path, monkeypatch):
    _, settings, manager = _setup(tmp_path)
    app = create_app(settings=settings, admin_config_manager=manager)
    with TestClient(app) as client:
        before = client.get("/admin/config", headers=_auth("admin-token")).json()
        draft = copy.deepcopy(before["config"])
        draft["rules"][0]["enabled"] = False
        token = app.state.runtime.bind_request(operation=True)
        try:
            busy = client.put("/admin/config", headers=_auth("admin-token"),
                              json={"revision": before["revision"], "config": draft})
        finally:
            app.state.runtime.reset_request(token)
            app.state.runtime.end_operation()
        assert busy.status_code == 409
        monkeypatch.setattr(app.state.runtime.current.agent, "pending_approvals", lambda: [object()])
        pending = client.put("/admin/config", headers=_auth("admin-token"),
                             json={"revision": before["revision"], "config": draft})
        assert pending.status_code == 409
        assert manager.revision() == before["revision"]


def test_request_keeps_old_snapshot_while_new_requests_see_hot_config(tmp_path):
    _, settings, manager = _setup(tmp_path)
    app = create_app(settings=settings, admin_config_manager=manager)
    with TestClient(app) as client:
        before = client.get("/admin/config", headers=_auth("admin-token")).json()
        draft = copy.deepcopy(before["config"])
        draft["settings"]["auth_enabled"] = False
        token = app.state.runtime.bind_request()
        try:
            assert app.state.settings.auth.enabled is True
            response = client.put("/admin/config", headers=_auth("admin-token"),
                                  json={"revision": before["revision"], "config": draft})
            assert response.status_code == 200, response.text
            assert app.state.settings.auth.enabled is True
        finally:
            app.state.runtime.reset_request(token)
        assert app.state.settings.auth.enabled is False
        assert client.get("/hosts").status_code == 200


def test_hot_update_keeps_one_inspection_thread(tmp_path):
    _, settings, manager = _setup(tmp_path)
    app = create_app(settings=settings, admin_config_manager=manager)
    with TestClient(app) as client:
        scheduler = app.state.runtime.scheduler
        thread = scheduler._thread
        before = client.get("/admin/config", headers=_auth("admin-token")).json()
        draft = copy.deepcopy(before["config"])
        draft["settings"]["inspection_enabled"] = True
        response = client.put("/admin/config", headers=_auth("admin-token"),
                              json={"revision": before["revision"], "config": draft})
        assert response.status_code == 200, response.text
        assert scheduler._thread is thread
        assert scheduler.running


def test_metrics_file_changes_disk_revision_and_can_be_applied(tmp_path):
    paths, settings, _old_manager = _setup(tmp_path)
    metrics = tmp_path / "metrics.yaml"
    metrics.write_bytes((Path(__file__).parents[1] / "config" / "metrics.yaml").read_bytes())
    with paths["settings"].open("a", encoding="utf-8") as handle:
        handle.write("metrics:\n  file: '" + metrics.as_posix() + "'\n")
    settings = replace(settings, metrics=MetricsSettings(file=metrics.as_posix()))
    manager = AdminConfigManager(
        settings, settings_file=paths["settings"], hosts_file=paths["hosts"],
        rules_file=paths["rules"], actions_file=paths["actions"],
        agent_actions_file=paths["agent_actions"], diagnostics_file=paths["diagnostics"],
        backup_root=tmp_path / "backups",
    )
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        previous = manager.revision()
        with metrics.open("a", encoding="utf-8") as handle:
            handle.write("\n# manual metric note\n")
        assert manager.revision() != previous
        applied = client.post("/admin/config/apply", headers=_auth("admin-token"),
                              json={"revision": manager.revision()})
        assert applied.status_code == 200, applied.text
        assert applied.json()["restart_required"] is False


def test_lazy_diagnostics_cannot_read_unapplied_disk_changes(tmp_path):
    paths, settings, manager = _setup(tmp_path)
    app = create_app(settings=settings, admin_config_manager=manager)
    with TestClient(app) as client:
        old_container = app.state.runtime.current.container
        paths["diagnostics"].write_text(
            "enabled: true\nprovider: ssh\ntarget: {host: localhost}\n"
            "commands:\n  memory: {summary: memory, template: 'free -m', params: {}}\n",
            encoding="utf-8",
        )
        assert old_container.diagnostics() is None
        disk = client.get("/admin/config", headers=_auth("admin-token")).json()
        assert disk["restart_required"] is True
        response = client.post("/admin/config/apply", headers=_auth("admin-token"),
                               json={"revision": disk["revision"]})
        assert response.status_code == 200, response.text
        assert app.state.runtime.current.container is not old_container
        assert app.state.runtime.current.container.diagnostics() is not None


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_admin_api_is_independent_from_regular_auth_switch(tmp_path):
    _, settings, manager = _setup(tmp_path, auth_enabled=False)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        assert client.get("/admin/config").status_code == 401
        assert client.get("/admin/config", headers=_auth("user-token")).status_code == 403
        response = client.get("/admin/config", headers=_auth("admin-token"))

    assert response.status_code == 200
    assert response.json()["available_identities"] == ["admin", "alertmanager", "alice"]


def test_admin_snapshot_is_structured_and_never_exposes_targets_or_secrets(tmp_path):
    _, settings, manager = _setup(tmp_path)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        body = client.get("/admin/config", headers=_auth("admin-token")).json()

    text = json.dumps(body, ensure_ascii=False)
    assert "admin-token" not in text
    assert "super-secret" not in text
    assert "10.0.0.10" not in text
    assert "known_hosts" not in text
    assert body["config"]["rules"][0]["host_id"] == "node-a"
    assert body["config"]["rules"][0]["params"] == {"service": "nginx"}


def test_admin_save_preserves_comments_backs_up_and_applies_immediately(tmp_path):
    paths, settings, manager = _setup(tmp_path)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        before = client.get("/admin/config", headers=_auth("admin-token")).json()
        draft = copy.deepcopy(before["config"])
        draft["settings"]["llm_enabled"] = False
        draft["rules"][0]["enabled"] = False
        response = client.put(
            "/admin/config", headers=_auth("admin-token"),
            json={"revision": before["revision"], "config": draft},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restart_required"] is False
    assert body["active_revision"] == body["revision"]
    assert {"settings.yaml", "rules.yaml"} <= {Path(name).name for name in body["changed_files"]}
    assert body["backup_id"]
    assert "# keep-settings-comment" in paths["settings"].read_text(encoding="utf-8")
    assert "# keep-rules-comment" in paths["rules"].read_text(encoding="utf-8")
    assert (tmp_path / "backups" / body["backup_id"] / "settings.yaml").is_file()
    assert "super-secret" in paths["rules"].read_text(encoding="utf-8")


def test_admin_save_switches_regular_auth_but_keeps_admin_gate(tmp_path):
    _, settings, manager = _setup(tmp_path)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        before = client.get("/admin/config", headers=_auth("admin-token")).json()
        draft = copy.deepcopy(before["config"])
        draft["settings"]["auth_enabled"] = False
        saved = client.put("/admin/config", headers=_auth("admin-token"),
                           json={"revision": before["revision"], "config": draft})
        assert saved.status_code == 200, saved.text
        assert client.get("/hosts").status_code == 200
        assert client.get("/admin/config").status_code == 401


def test_manual_disk_apply_uses_revision_and_rejects_restart_only_fields(tmp_path):
    paths, settings, manager = _setup(tmp_path)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        path = paths["settings"]
        original = path.read_text(encoding="utf-8")
        path.write_text(original.replace("enabled: true\nagent:", "enabled: false\nagent:", 1), encoding="utf-8")
        disk = client.get("/admin/config", headers=_auth("admin-token")).json()
        assert disk["restart_required"] is True
        stale = client.post("/admin/config/apply", headers=_auth("admin-token"),
                            json={"revision": "0" * 64})
        assert stale.status_code == 409
        applied = client.post("/admin/config/apply", headers=_auth("admin-token"),
                              json={"revision": disk["revision"]})
        assert applied.status_code == 200, applied.text
        assert applied.json()["restart_required"] is False
        path.write_text(path.read_text(encoding="utf-8") + "server:\n  port: 8001\n", encoding="utf-8")
        new_disk = client.get("/admin/config", headers=_auth("admin-token")).json()
        rejected = client.post("/admin/config/apply", headers=_auth("admin-token"),
                               json={"revision": new_disk["revision"]})
        assert rejected.status_code == 422
        assert "server" in rejected.json()["detail"]
        assert manager.runtime.current.revision == applied.json()["revision"]


def test_admin_save_rejects_stale_revision_and_cross_file_dangling_reference(tmp_path):
    _, settings, manager = _setup(tmp_path)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        snapshot = client.get("/admin/config", headers=_auth("admin-token")).json()
        dangling = copy.deepcopy(snapshot["config"])
        dangling["catalogs"]["rule_actions"]["commands"] = []
        invalid = client.put(
            "/admin/config", headers=_auth("admin-token"),
            json={"revision": snapshot["revision"], "config": dangling},
        )
        stale = client.put(
            "/admin/config", headers=_auth("admin-token"),
            json={"revision": "0" * 64, "config": snapshot["config"]},
        )

    assert invalid.status_code == 422
    assert "未知" in invalid.json()["detail"]
    assert stale.status_code == 409


def test_admin_batch_write_rolls_back_files_when_replace_fails(tmp_path, monkeypatch):
    paths, _, manager = _setup(tmp_path)
    snapshot = manager.snapshot()
    draft = copy.deepcopy(snapshot["config"])
    draft["settings"]["llm_enabled"] = False
    draft["rules"][0]["enabled"] = False
    originals = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}
    real_write = manager._atomic_write
    calls = 0

    def flaky_write(path, text):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replace failure")
        real_write(path, text)

    monkeypatch.setattr(manager, "_atomic_write", flaky_write)
    try:
        manager.update(expected_revision=snapshot["revision"], config=draft, actor="admin")
    except OSError as exc:
        assert "simulated" in str(exc)
    else:  # pragma: no cover - 防止故障注入失效
        raise AssertionError("预期写入失败")

    assert {name: path.read_text(encoding="utf-8") for name, path in paths.items()} == originals
