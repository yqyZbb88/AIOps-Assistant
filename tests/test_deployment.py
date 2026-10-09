"""部署初始化、升级和凭据处理全部使用临时目录，不启动外部服务。"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import deployment, stack
from src.utils.admin_config import AdminConfigManager
from src.utils.config_loader import _build_settings, interpolate

TEMPLATES = Path(__file__).resolve().parents[1] / "deploy" / "config"


def initialize(runtime, **kwargs):
    return deployment.initialize(runtime, templates=TEMPLATES, environment=kwargs)


def test_fresh_deployment_secure_consistent_and_idempotent(tmp_path):
    assert initialize(tmp_path) == "container"
    before = deployment.read_credentials(tmp_path)
    derived_before = (tmp_path / "secrets/assistant.json").read_bytes()
    config = yaml.safe_load((tmp_path / "config/settings.yaml").read_text(encoding="utf-8"))
    assert "enabled" not in config["auth"]
    hosts = yaml.safe_load((tmp_path / "config/hosts.yaml").read_text(encoding="utf-8"))
    assert hosts["hosts"][0]["prometheus"] == {"job": "node-exporter", "instance": "node-exporter:9100"}
    assert "ssh" not in hosts["hosts"][0]
    assert initialize(tmp_path) == "container"
    assert deployment.read_credentials(tmp_path) == before
    assert (tmp_path / "secrets/assistant.json").read_bytes() == derived_before
    assert json.loads(before["OPS_API_TOKENS"])[before["AIOPS_USER_TOKEN"]] == "admin"
    webhook = (tmp_path / "alertmanager/token").read_text()
    assert json.loads(before["OPS_API_TOKENS"])[webhook] == "alertmanager"
    assert webhook != before["AIOPS_USER_TOKEN"]
    assert (tmp_path / "grafana/admin_password").read_text() == before["GF_SECURITY_ADMIN_PASSWORD"]


def test_reinitialize_preserves_user_config_targets_and_demo(tmp_path):
    initialize(tmp_path)
    for name in ("config/settings.yaml", "prometheus-targets/assistant.yml", "textfile/aiops_demo_fault.prom"):
        (tmp_path / name).write_text("user changes", encoding="utf-8")
    initialize(tmp_path)
    for name in ("config/settings.yaml", "prometheus-targets/assistant.yml", "textfile/aiops_demo_fault.prom"):
        assert (tmp_path / name).read_text() == "user changes"


def test_missing_derived_secrets_are_repaired_without_rotation(tmp_path):
    initialize(tmp_path)
    original = deployment.read_credentials(tmp_path)
    for name in ("secrets/assistant.json", "alertmanager/token", "grafana/admin_password"):
        (tmp_path / name).unlink()
    initialize(tmp_path)
    assert deployment.read_credentials(tmp_path) == original
    assert json.loads((tmp_path / "secrets/assistant.json").read_text())["OPS_API_TOKENS"] == original["OPS_API_TOKENS"]


def test_missing_source_credentials_do_not_rotate_initialized_deployment(tmp_path):
    initialize(tmp_path)
    before = (tmp_path / "secrets/assistant.json").read_bytes()
    (tmp_path / "compose.env").unlink()
    with pytest.raises(deployment.DeploymentError, match="丢失"):
        initialize(tmp_path)
    assert (tmp_path / "secrets/assistant.json").read_bytes() == before


def test_legacy_upgrade_preserves_tokens_config_and_migrates_server(tmp_path, monkeypatch):
    initialize(tmp_path)
    original_credentials = deployment.read_credentials(tmp_path)
    settings = tmp_path / "config/settings.yaml"
    settings.write_text(settings.read_text(encoding="utf-8") + "server:\n  host: 127.0.0.1\n  port: 9000\n", encoding="utf-8")
    before = settings.read_text(encoding="utf-8")
    (tmp_path / "deployment.json").write_text(json.dumps({"version": 1, "config_source": "legacy", "initialized": True}))
    assert initialize(tmp_path) == "container"
    assert deployment.read_credentials(tmp_path) == original_credentials
    assert "server" not in yaml.safe_load(settings.read_text(encoding="utf-8"))
    backups = list((tmp_path / "assistant-data/config-backups").glob("obsolete-settings-*.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == before
    assert json.loads((tmp_path / "deployment.json").read_text())["version"] == 2
    # Quoted environment placeholders must remain strings after the migration round trip.
    for key, value in json.loads((tmp_path / "secrets/assistant.json").read_text()).items():
        monkeypatch.setenv(key, value)
    loaded = _build_settings(yaml.safe_load(interpolate(settings.read_text(encoding="utf-8"))))
    assert loaded.auth.tokens == original_credentials["OPS_API_TOKENS"]
    assert loaded.app.version == "0.6.2"


def test_initialized_missing_config_does_not_apply_defaults(tmp_path):
    initialize(tmp_path)
    before = (tmp_path / "compose.env").read_bytes()
    (tmp_path / "config/hosts.yaml").unlink()
    with pytest.raises(deployment.DeploymentError, match="备份"):
        initialize(tmp_path)
    assert (tmp_path / "compose.env").read_bytes() == before
    assert not (tmp_path / "config/hosts.yaml").exists()


def test_corrupt_existing_credentials_fail_without_overwrite(tmp_path):
    original = 'OPS_API_TOKENS={"do-not-expose":"admin"}\n'
    (tmp_path / "compose.env").write_text(original)
    with pytest.raises(deployment.DeploymentError) as error:
        initialize(tmp_path)
    assert "do-not-expose" not in str(error.value)
    assert (tmp_path / "compose.env").read_text() == original


def test_model_settings_roundtrip_literal_values_and_update(tmp_path):
    key = "test-$literal-'quoted'-\\value"
    initialize(tmp_path, DEEPSEEK_API_KEY=key, DEEPSEEK_MODEL="model-one")
    assert deployment.read_credentials(tmp_path)["DEEPSEEK_API_KEY"] == key
    initialize(tmp_path, DEEPSEEK_API_KEY="", DEEPSEEK_MODEL="model-two")
    values = deployment.read_credentials(tmp_path)
    assert values["DEEPSEEK_API_KEY"] == key
    assert values["DEEPSEEK_MODEL"] == "model-two"


def test_template_set_passes_application_cross_validation(tmp_path, monkeypatch):
    initialize(tmp_path)
    values = json.loads((tmp_path / "secrets/assistant.json").read_text())
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("OPS_LLM_ENABLED", "false")
    monkeypatch.setenv("OPS_AGENT_ENABLED", "false")
    text = (tmp_path / "config/settings.yaml").read_text(encoding="utf-8")
    settings = _build_settings(yaml.safe_load(interpolate(text)))
    paths = {name[:-5] + "_file": tmp_path / "config" / name for name in deployment.CONFIG_FILES}
    manager = AdminConfigManager(settings, **paths)
    manager._validate(manager._load_docs())
    assert not settings.agent.enabled and not settings.llm.enabled


def test_serve_sets_optional_ai_and_single_worker(tmp_path, monkeypatch):
    path = tmp_path / "secrets.json"
    path.write_text(json.dumps({"DEEPSEEK_API_KEY": "example-key"}))
    for key in ("OPS_LLM_ENABLED", "OPS_AGENT_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    calls = []
    monkeypatch.setattr(deployment.os, "execvp", lambda *args: calls.append(args))
    with monkeypatch.context() as context:
        context.setattr(deployment.os, "environ", {})
        deployment.serve(path)
        assert deployment.os.environ["OPS_LLM_ENABLED"] == "true"
    assert calls[0][1][-2:] == ["--workers", "1"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX bind mount modes")
def test_public_service_files_readable_but_admin_secrets_private(tmp_path):
    initialize(tmp_path)
    assert (tmp_path / "alertmanager/token").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "grafana/admin_password").stat().st_mode & 0o777 == 0o644
    assert (tmp_path / "secrets/assistant.json").stat().st_mode & 0o777 == 0o600
    assert tmp_path.stat().st_mode & 0o777 == 0o700
    path = tmp_path / "prometheus-targets/assistant.yml"
    AdminConfigManager._atomic_write(path, "[]\n")
    assert path.stat().st_mode & 0o777 == 0o644


def test_read_only_helper_uses_compose_paths_without_initializing(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="test-token\n")
    monkeypatch.setattr(stack.subprocess, "run", run)
    assert stack.compose_secret() == "test-token"
    assert calls[0][-5:] == ["run", "--rm", "--no-deps", "init", "show-token"]


def test_doctor_reports_daemon_failure_without_echoing_secrets(monkeypatch):
    monkeypatch.setattr(stack.shutil, "which", lambda _: "/docker")
    responses = iter([SimpleNamespace(returncode=0, stdout="v2.24.0"), SimpleNamespace(returncode=1, stdout="secret")])
    monkeypatch.setattr(stack.subprocess, "run", lambda *a, **k: next(responses))
    with pytest.raises(RuntimeError, match="引擎未就绪"):
        stack.doctor()


def test_up_waits_for_health_and_does_not_reset_demo(monkeypatch):
    calls = []
    monkeypatch.setattr(stack, "doctor", lambda: None)
    monkeypatch.setattr(stack, "compose", lambda *args: calls.append(args) or 0)
    monkeypatch.setattr("sys.argv", ["stack.py", "up", "--wait-timeout", "240"])
    assert stack.main() == 0
    assert calls == [("up", "--build", "--wait", "--wait-timeout", "240")]


@pytest.mark.parametrize("legacy", [False, True])
def test_interrupted_initialization_missing_credentials_does_not_rotate(tmp_path, legacy):
    runtime = tmp_path / "runtime"
    if legacy:
        initialize(runtime)
        (runtime / "deployment.json").unlink()
        (runtime / "secrets/assistant.json").unlink()
        for name in deployment.CONFIG_FILES:
            (runtime / "config" / name).unlink()
    missing_templates = tmp_path / "missing-templates"
    with pytest.raises(deployment.DeploymentError, match="缺少初始化配置"):
        deployment.initialize(runtime, templates=missing_templates, environment={})
    (runtime / "compose.env").unlink()
    with pytest.raises(deployment.DeploymentError, match="丢失"):
        initialize(runtime)
    assert not (runtime / "compose.env").exists()


def test_interrupted_initialization_retries_with_same_credentials(tmp_path):
    runtime = tmp_path / "runtime"
    missing = tmp_path / "missing-templates"
    with pytest.raises(deployment.DeploymentError, match="缺少初始化配置"):
        deployment.initialize(runtime, templates=missing, environment={})
    original = (runtime / "compose.env").read_bytes()
    assert initialize(runtime) == "container"
    assert (runtime / "compose.env").read_bytes() == original
    assert json.loads((runtime / "deployment.json").read_text())["initialized"] is True


def test_pre_checkpoint_interrupted_initialization_does_not_rotate(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "deployment.json").write_text(json.dumps({"version": 1, "config_source": "bundled"}))
    (runtime / "config").mkdir()
    (runtime / "config/settings.yaml").write_text("app: {}\n")
    with pytest.raises(deployment.DeploymentError, match="丢失"):
        initialize(runtime)
    assert not (runtime / "compose.env").exists()


def test_fresh_initialization_allows_precreated_compose_mounts(tmp_path):
    runtime = tmp_path / "runtime"
    for name in ("config", "secrets", "assistant-data", "assistant-logs"):
        (runtime / name).mkdir(parents=True)
    assert initialize(runtime) == "container"
    assert (runtime / "compose.env").is_file()



def test_failed_precredential_validation_can_retry_with_empty_mounts(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / "config").mkdir(parents=True)
    missing = tmp_path / "missing"
    with pytest.raises(deployment.DeploymentError, match="单行"):
        deployment.initialize(runtime, templates=missing,
                              environment={"DEEPSEEK_API_KEY": "synthetic\ninvalid"})
    assert not (runtime / "compose.env").exists()
    assert initialize(runtime) == "container"


def test_initialization_preserves_page_credentials_and_assets(tmp_path):
    initialize(tmp_path)
    directory = tmp_path / "assistant-data" / "admin-credentials"
    (directory / "assets" / "fixture").mkdir(parents=True)
    (directory / "state.json").write_text("page-managed-state", encoding="utf-8")
    (directory / "assets" / "fixture" / "private_key").write_text("isolated-test-key", encoding="utf-8")
    initialize(tmp_path)
    assert (directory / "state.json").read_text() == "page-managed-state"
    assert (directory / "assets" / "fixture" / "private_key").read_text() == "isolated-test-key"


@pytest.mark.parametrize("legacy_auth", [False, True, "invalid-old-value"])
def test_upgrade_removes_obsolete_fields_without_resetting_acl_or_credentials(tmp_path, legacy_auth):
    initialize(tmp_path)
    path = tmp_path / "config/settings.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["auth"]["enabled"] = legacy_auth
    doc["auth"]["host_access"] = '{"alice":["local-node"]}'
    doc.setdefault("app", {})["version"] = "outdated"
    doc["alert"]["trend_step"] = "5m"
    doc["server"] = {"host": "127.0.0.1", "port": 9000}
    doc["unrecognized"] = {"keep_for_validation": True}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    before = path.read_bytes()
    credentials = deployment.read_credentials(tmp_path)
    initialize(tmp_path)
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected = yaml.safe_load(before)
    del expected["auth"]["enabled"]
    del expected["app"]["version"]
    del expected["alert"]["trend_step"]
    del expected["server"]
    assert result == expected
    assert deployment.read_credentials(tmp_path) == credentials
    backups = list((tmp_path / "assistant-data/config-backups").glob("obsolete-settings-*.yaml"))
    assert len(backups) == 1 and backups[0].read_bytes() == before
    saved = path.read_bytes()
    initialize(tmp_path)
    assert path.read_bytes() == saved
    assert list((tmp_path / "assistant-data/config-backups").glob("obsolete-settings-*.yaml")) == backups


@pytest.mark.parametrize("failure", ["backup", "settings"])
def test_obsolete_settings_migration_write_failure_preserves_original(tmp_path, monkeypatch, failure):
    initialize(tmp_path)
    path = tmp_path / "config/settings.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "server: {}\n", encoding="utf-8")
    before = path.read_bytes()
    original_write = deployment.atomic_write
    def fail(target, content, **kwargs):
        if (failure == "settings" and target == path) or (failure == "backup" and target.name.startswith("obsolete-settings-")):
            raise OSError("synthetic sensitive context")
        return original_write(target, content, **kwargs)
    monkeypatch.setattr(deployment, "atomic_write", fail)
    with pytest.raises(deployment.DeploymentError) as caught:
        deployment.migrate_container_settings(tmp_path)
    assert "sensitive" not in str(caught.value)
    assert path.read_bytes() == before
