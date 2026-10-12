"""一次性迁移验收：预览、冲突、备份、发布回滚及中断恢复。"""
from pathlib import Path
import json
import pytest
from scripts import deployment
from src.utils import config_migration as migration

TEMPLATES = Path(__file__).parents[1] / "deploy/templates/assistant"


@pytest.fixture
def old(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    runtime = project / ".runtime"
    deployment.initialize(runtime, templates=TEMPLATES, environment={})
    path = runtime / "config/settings.yaml"
    path.write_text(path.read_text().replace("openai_compatible", "deepseek"))
    for name in ("diagnostics", "agent_actions"):
        path = runtime / "config" / (name + ".yaml")
        path.write_text(path.read_text() + "\ntarget: {host: unused-old-host}\n")
    with (runtime / "compose.env").open("a") as handle:
        handle.write('DEEPSEEK_API_KEY="synthetic-old-key"\nSSH_HOST="unused-old-host"\nSSH_PASSWORD="synthetic-password"\n')
    (project / ".env").write_text('# 保留注释\nDEEPSEEK_MODEL="old-model" # comment\nSSH_HOST=unused-old-host\nASSISTANT_HOST_PORT=18000\n')
    return runtime, project


def snapshot(project):
    return {str(p.relative_to(project)): p.read_bytes() for p in project.rglob("*")
            if p.is_file() and "config-migrations" not in p.parts and p.name != ".init.lock"}


def test_dry_run_and_idempotence(old):
    runtime, project = old
    before = snapshot(project)
    assert "@env" in migration.migrate(runtime, project, dry_run=True)
    assert snapshot(project) == before
    original = migration._dotenv((runtime / "compose.env").read_text())
    migration.migrate(runtime, project)
    values = deployment.read_credentials(runtime)
    assert values["MODEL_API_KEY"] == "synthetic-old-key"
    assert values["MODEL_BASE_URL"] == "https://api.deepseek.com"
    assert values["OPS_API_TOKENS"] == original["OPS_API_TOKENS"]
    assert values["GF_SECURITY_ADMIN_PASSWORD"] == original["GF_SECURITY_ADMIN_PASSWORD"]
    assert not any(k.startswith(("DEEPSEEK_", "SSH_")) for k in values)
    assert 'MODEL_NAME="old-model" # comment' in (project / ".env").read_text()
    assert "# 保留注释" in (project / ".env").read_text()
    assert migration.migrate(runtime, project) == []
    deployment.initialize(runtime, templates=TEMPLATES, environment={})


def test_conflict_has_no_published_changes(old):
    runtime, project = old
    with (project / ".env").open("a") as handle:
        handle.write("MODEL_NAME=conflicting-name\n")
    before = snapshot(project)
    with pytest.raises(ValueError, match="冲突") as error:
        migration.migrate(runtime, project)
    assert "conflicting-name" not in str(error.value)
    assert snapshot(project) == before


def test_old_startup_is_rejected_without_writes(old):
    runtime, project = old
    before = snapshot(project)
    with pytest.raises(ValueError, match="migrate-config"):
        deployment.initialize(runtime, templates=TEMPLATES, environment={})
    assert snapshot(project) == before


def test_existing_page_model_is_preserved(old):
    runtime, project = old
    directory = runtime / "assistant-data/admin-credentials"
    directory.mkdir(parents=True)
    state = {"version": 1, "tokens": {}, "ssh": {}, "model": {"api_key": "synthetic-page-key",
        "base_url": "https://page.invalid/v1", "model": "page-model", "timeout": 12, "max_retries": 0, "max_tokens": 256}}
    (directory / "state.json").write_text(json.dumps(state))
    migration.migrate(runtime, project)
    saved = json.loads((directory / "state.json").read_text())
    assert saved["model"]["api_key"] == "synthetic-page-key"
    assert saved["model"]["base_url"] == "https://page.invalid/v1"
    assert saved["model"]["token_limit_param"] == "max_tokens"


@pytest.mark.parametrize("failure", ["backup", "publish"])
def test_write_failure_restores_original(old, monkeypatch, failure):
    runtime, project = old
    before = snapshot(project)
    original = migration._atomic
    failed = False
    def failing(path, content, *args, **kwargs):
        nonlocal failed
        selected = path.name.endswith(".before") if failure == "backup" else path == runtime / "config/diagnostics.yaml"
        if selected and not failed:
            failed = True
            raise OSError("synthetic-sensitive")
        return original(path, content, *args, **kwargs)
    monkeypatch.setattr(migration, "_atomic", failing)
    with pytest.raises((ValueError, OSError)):
        migration.migrate(runtime, project)
    assert snapshot(project) == before


def test_interruption_blocks_startup_and_rerun_recovers(old, monkeypatch):
    runtime, project = old
    original = migration._atomic
    def interrupt(path, content, *args, **kwargs):
        if path == runtime / "config/diagnostics.yaml":
            raise KeyboardInterrupt
        return original(path, content, *args, **kwargs)
    monkeypatch.setattr(migration, "_atomic", interrupt)
    with pytest.raises(KeyboardInterrupt):
        migration.migrate(runtime, project)
    with pytest.raises(ValueError, match="迁移未完成"):
        migration.check_runtime(runtime)
    monkeypatch.setattr(migration, "_atomic", original)
    migration.migrate(runtime, project)
    migration.check_runtime(runtime)
    assert migration.migrate(runtime, project) == []


def test_interrupted_migration_refuses_external_changes(old, monkeypatch):
    runtime, project = old
    original = migration._atomic
    def interrupt(path, content, *args, **kwargs):
        if path == runtime / "config/diagnostics.yaml":
            raise KeyboardInterrupt
        return original(path, content, *args, **kwargs)
    monkeypatch.setattr(migration, "_atomic", interrupt)
    with pytest.raises(KeyboardInterrupt):
        migration.migrate(runtime, project)
    path = runtime / "config/settings.yaml"
    path.write_text(path.read_text() + "\n# manual edit\n")
    before = path.read_bytes()
    monkeypatch.setattr(migration, "_atomic", original)
    with pytest.raises(ValueError, match="又被修改"):
        migration.migrate(runtime, project)
    assert path.read_bytes() == before


def test_missing_material_blocks_publish(old):
    runtime, project = old
    path = runtime / "config/hosts.yaml"
    path.write_text(path.read_text().replace("credentials: {}", 'credentials: {old: {username: ops, password: "${SSH_PASSWORD}", known_hosts: "/run/ssh/missing"}}'))
    before = snapshot(project)
    with pytest.raises(ValueError):
        migration.migrate(runtime, project)
    assert snapshot(project) == before


@pytest.mark.parametrize("method", ["password", "private_key"])
def test_environment_ssh_becomes_managed_credential(old, method):
    import paramiko
    runtime, project = old
    materials = runtime / "ssh"
    materials.mkdir()
    key = paramiko.RSAKey.generate(1024)
    known = "test-host " + key.get_name() + " " + key.get_base64() + "\n"
    (materials / "known_hosts").write_text(known)
    path = runtime / "config/hosts.yaml"
    path.write_text("""version: 1
credentials:
  old:
    username: ops
    password: "${SSH_PASSWORD}"
    known_hosts: "${SSH_KNOWN_HOSTS}"
hosts:
  - id: local-node
    name: Test host
    prometheus: {job: node-exporter, instance: "test-host:9100"}
    ssh: {host: test-host, credential_ref: old}
    allow: {diagnostics: [], rule_actions: [], agent_actions: []}
""")
    with (runtime / "compose.env").open("a") as handle:
        handle.write('SSH_KNOWN_HOSTS="/run/ssh/known_hosts"\n')
    if method == "private_key":
        key.write_private_key_file(str(materials / "private_key"))
        path.write_text(path.read_text().replace('password: "' + chr(36) + '{SSH_PASSWORD}"', 'key_filename: "/run/ssh/private_key"'))
    migration.migrate(runtime, project)
    state = json.loads((runtime / "assistant-data/admin-credentials/state.json").read_text())
    assert len(state["ssh"]) == 1
    assert next(iter(state["ssh"].values()))["known_hosts"] == known
    assert "synthetic-password" not in path.read_text()
    assert "SSH_" not in path.read_text()


def test_new_deployment_keeps_empty_model_defaults(tmp_path):
    runtime = tmp_path / "runtime"
    project = tmp_path / "project"
    project.mkdir()
    deployment.initialize(runtime, templates=TEMPLATES, environment={})
    migration.migrate(runtime, project)
    values = deployment.read_credentials(runtime)
    assert not values.get("MODEL_BASE_URL") and not values.get("MODEL_NAME") and not values.get("MODEL_API_KEY")
    assert migration.migrate(runtime, project) == []


def test_identical_aliases_are_merged(old):
    runtime, project = old
    with (project / ".env").open("a") as handle:
        handle.write("MODEL_NAME=old-model\n")
    migration.migrate(runtime, project)
    assert migration._dotenv((project / ".env").read_text())["MODEL_NAME"] == "old-model"


@pytest.mark.parametrize("kind", ["unknown_field", "unmapped_host", "invalid_credential", "deployment_credentials", "deployment_metadata"])
def test_invalid_candidate_publishes_nothing(old, kind):
    runtime, project = old
    if kind == "unknown_field":
        with (runtime / "config/settings.yaml").open("a") as handle:
            handle.write("unknown_section: true\n")
    elif kind == "unmapped_host":
        path = runtime / "config/settings.yaml"
        path.write_text(path.read_text().replace("file: config/hosts.yaml", 'file: ""'))
        path = runtime / "config/diagnostics.yaml"
        path.write_text(path.read_text().replace("enabled: false", "enabled: true"))
    elif kind == "deployment_credentials":
        path = runtime / "compose.env"
        path.write_text("\n".join(line for line in path.read_text().splitlines() if not line.startswith("GF_SECURITY_ADMIN_PASSWORD=")) + "\n")
    elif kind == "deployment_metadata":
        (runtime / "deployment.json").write_text('{"version": 999}')
    else:
        directory = runtime / "assistant-data/admin-credentials"
        directory.mkdir(parents=True)
        (directory / "state.json").write_text('{"version": 999, "secret": "sensitive-fixture"}')
    before = snapshot(project)
    with pytest.raises(ValueError) as error:
        migration.migrate(runtime, project)
    assert "sensitive-fixture" not in str(error.value)
    assert snapshot(project) == before


def test_root_cannot_override_deployment_identity_source(old):
    runtime, project = old
    original = migration._dotenv((runtime / "compose.env").read_text())["OPS_API_TOKENS"]
    with (project / ".env").open("a") as handle:
        handle.write("OPS_API_TOKENS=not-an-auth-source\n")
    migration.migrate(runtime, project)
    assert deployment.read_credentials(runtime)["OPS_API_TOKENS"] == original


def test_invalid_page_ssh_material_blocks_publish(old):
    runtime, project = old
    directory = runtime / "assistant-data/admin-credentials"
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps({"version": 1, "tokens": {}, "model": None, "ssh": {
        "old-key": {"username": "ops", "password": "synthetic", "private_key": "", "known_hosts": "invalid-host-material"}}}))
    before = snapshot(project)
    with pytest.raises(ValueError):
        migration.migrate(runtime, project)
    assert snapshot(project) == before


def test_cross_file_alias_conflict_has_no_writes(old):
    runtime, project = old
    with (project / ".env").open("a") as handle:
        handle.write("MODEL_API_KEY=new-sensitive-fixture\n")
    before = snapshot(project)
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY / MODEL_API_KEY") as error:
        migration.migrate(runtime, project)
    assert "sensitive-fixture" not in str(error.value)
    assert snapshot(project) == before


def test_env_without_final_newline_keeps_separate_new_fields(old):
    runtime, project = old
    (project / ".env").write_text("ASSISTANT_HOST_PORT=18000")
    path = runtime / "compose.env"
    path.write_text(path.read_text().rstrip("\n"))
    migration.migrate(runtime, project)
    assert migration._dotenv((project / ".env").read_text()) == {"ASSISTANT_HOST_PORT": "18000"}
    assert deployment.read_credentials(runtime)["MODEL_BASE_URL"] == "https://api.deepseek.com"


def test_only_old_persistent_environment_blocks_startup_before_writes(tmp_path):
    runtime = tmp_path / "runtime"
    deployment.initialize(runtime, templates=TEMPLATES, environment={})
    with (runtime / "compose.env").open("a") as handle:
        handle.write('DEEPSEEK_API_KEY="synthetic"\n')
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="migrate-config"):
        deployment.initialize(runtime, templates=TEMPLATES, environment={})
    assert snapshot(tmp_path) == before
