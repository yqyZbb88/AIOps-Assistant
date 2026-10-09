"""管理员凭据的隔离离线验收，不连接 SSH 或模型。"""
import io
import json
import os
from types import SimpleNamespace

import paramiko
import pytest
from fastapi.testclient import TestClient

from tests.test_admin_config import _setup, _auth
from src.main import create_app
from src.utils.admin_credentials import AdminCredentials, CredentialStore, validate_ssh


def update(client, path, revision, data=None, method="PUT"):
    return client.request(method, "/admin/credentials/" + path, headers=_auth("admin-token"),
                          json={"revision": revision, "data": data or {}})


def get(client):
    return client.get("/admin/credentials", headers=_auth("admin-token"))


@pytest.fixture
def trusted_key():
    key = paramiko.RSAKey.generate(1024)
    file = io.StringIO()
    key.write_private_key(file)
    return file.getvalue(), "10.0.0.10 ssh-rsa " + key.get_base64() + "\n"


def test_token_admin_gate_once_rotation_revocation_and_restart(tmp_path):
    paths, settings, manager = _setup(tmp_path)
    original = {key: file.read_bytes() for key, file in paths.items()}
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        assert client.get("/admin/credentials").status_code == 401
        assert client.get("/admin/credentials", headers=_auth("user-token")).status_code == 403
        snap = get(client).json()
        assert all(not item["managed"] for item in snap["tokens"])
        assert "admin-token" not in json.dumps(snap)
        result = update(client, "tokens", snap["revision"], {"identity": "bob"}, "POST")
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        issued = result.json(); first, token_id = issued["token_once"], issued["token_id"]
        assert first != "***" and len(first) >= 40
        assert client.get("/hosts", headers=_auth(first)).status_code == 200
        assert client.get("/admin/credentials", headers=_auth(first)).status_code == 403
        assert first not in get(client).text
        assert "token_once" not in get(client).json()
        assert update(client, "tokens", snap["revision"], {"identity": "stale"}, "POST").status_code == 409
        rotated = update(client, "tokens/" + token_id + "/rotate", issued["revision"], method="POST").json()
        second = rotated["token_once"]
        assert client.get("/hosts", headers=_auth(first)).status_code == 401
        assert client.get("/hosts", headers=_auth(second)).status_code == 200
        # 新进程重新加载；部署管理员与 webhook 映射仍保留。
        from src.utils.admin_config import AdminConfigManager
        recovered = AdminConfigManager(settings, settings_file=paths["settings"], hosts_file=paths["hosts"],
            rules_file=paths["rules"], actions_file=paths["actions"], agent_actions_file=paths["agent_actions"],
            diagnostics_file=paths["diagnostics"], backup_root=tmp_path / "backups")
        with TestClient(create_app(settings=settings, admin_config_manager=recovered)) as restarted:
            assert restarted.get("/hosts", headers=_auth(second)).status_code == 200
            assert dict(__import__("src.utils.auth", fromlist=["parse_tokens"]).parse_tokens(recovered.runtime.current.settings.auth.tokens))["alert-token"] == "alertmanager"
            revoked = update(restarted, "tokens/" + token_id, get(restarted).json()["revision"], method="DELETE")
            assert revoked.status_code == 200, revoked.text
            assert restarted.get("/hosts", headers=_auth(second)).status_code == 401
            assert get(restarted).status_code == 200
    assert original == {key: file.read_bytes() for key, file in paths.items()}


def test_ssh_assets_bind_conflict_no_secret_or_yaml_roundtrip(tmp_path, trusted_key, monkeypatch):
    paths, settings, manager = _setup(tmp_path)
    private, known = trusted_key
    original = paths["hosts"].read_bytes()
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        rev = get(client).json()["revision"]
        data = {"username": "operator", "method": "private_key", "private_key": private, "known_hosts": known}
        saved = update(client, "ssh/local-key", rev, data)
        assert saved.status_code == 200, saved.text
        assert private not in saved.text and known not in saved.text
        assert paths["hosts"].read_bytes() == original
        from src.utils.host_onboarding import HostOnboarding
        monkeypatch.setattr(HostOnboarding, "_targets", lambda self: [])
        options = client.get("/admin/hosts/options", headers=_auth("admin-token"))
        assert options.status_code == 200 and options.json()["credentials"] == ["local-key", "secret"]
        assert private not in options.text
        specs = manager.credential_store.ssh_specs(manager.credential_store.read())["local-key"]
        assert specs["key_filename"].startswith(str(manager.credential_store.directory))
        old_path = specs["known_hosts"]
        before = manager.runtime.current.container.hosts().get("node-a").capabilities
        bound = update(client, "hosts/node-a/ssh", saved.json()["revision"], {"credential_id": "local-key"})
        assert bound.status_code == 200, bound.text
        host = manager.runtime.current.container.hosts().get("node-a")
        assert host.credential_ref == "local-key" and host.capabilities == before
        assert update(client, "ssh/local-key", bound.json()["revision"], method="DELETE").status_code == 409
        assert private not in paths["hosts"].read_text() and known not in paths["hosts"].read_text()
        assert update(client, "ssh/secret", bound.json()["revision"], data).status_code == 422
        from pathlib import Path
        assert Path(old_path).exists()
        if os.name == "posix":
            assert Path(specs["key_filename"]).stat().st_mode & 0o777 == 0o600


def test_password_keep_and_unsupported_key_paths(tmp_path, trusted_key):
    paths, settings, manager = _setup(tmp_path)
    _, known = trusted_key
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        result = update(client, "ssh/pass", get(client).json()["revision"], {"username": "u", "method": "password", "password": "new-secret", "known_hosts": known})
        assert result.status_code == 200, result.text
        kept = update(client, "ssh/pass", result.json()["revision"], {"username": "v", "method": "password"})
        assert kept.status_code == 200, kept.text
        assert manager.credential_store.read()["ssh"]["pass"]["password"] == "new-secret"
        bad = update(client, "ssh/invalid", kept.json()["revision"], {"username": "v", "method": "private_key", "key_filename": "/etc/secret", "private_key": "private-bad", "known_hosts": known})
        assert bad.status_code == 422 and "private-bad" not in bad.text
        encrypted = io.StringIO(); paramiko.RSAKey.generate(1024).write_private_key(encrypted, password="passphrase")
        with pytest.raises(__import__("src.utils.admin_config", fromlist=["AdminConfigError"]).AdminConfigError, match="口令"):
            validate_ssh({"username": "v", "method": "private_key", "private_key": encrypted.getvalue(), "known_hosts": known}, None)


def test_model_save_lazy_keep_clear_cache_versions_and_explicit_test(tmp_path, monkeypatch):
    from src.utils import model_loader
    paths, settings, manager = _setup(tmp_path)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "env-model-secret")
    calls = []
    monkeypatch.setattr(model_loader, "get_model", lambda config=None: SimpleNamespace(invoke=lambda *a, **k: calls.append(config) or SimpleNamespace(content="OK")))
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        snap = get(client).json(); assert snap["model"]["configured"]
        data = {"key_action": "replace", "api_key": "managed-model-secret", "model": "deepseek-chat", "base_url": "https://model.example/v1", "timeout": 12, "max_retries": 1, "max_tokens": 200}
        saved = update(client, "model", snap["revision"], data)
        assert saved.status_code == 200, saved.text
        assert "managed-model-secret" not in saved.text and not calls
        old_container = manager.runtime.current.container
        assert old_container._model_config["api_key"] == "managed-model-secret"
        kept = update(client, "model", saved.json()["revision"], {"key_action": "keep", "model": "deepseek-v4-flash"})
        assert kept.status_code == 200, kept.text
        assert old_container._model_config["model"] == "deepseek-chat"
        assert client.post("/admin/credentials/model/test", headers=_auth("user-token")).status_code == 403
        tested = client.post("/admin/credentials/model/test", headers=_auth("admin-token"))
        assert tested.status_code == 200 and len(calls) == 1
        cleared = update(client, "model", kept.json()["revision"], {"key_action": "clear"})
        assert cleared.status_code == 200 and not cleared.json()["model"]["configured"]
        assert manager.runtime.current.container._model_config["api_key"] == ""
        assert client.post("/admin/credentials/model/test", headers=_auth("admin-token")).status_code == 422
        assert "managed-model-secret" not in paths["settings"].read_text()


def test_failure_rolls_back_busy_and_corrupt_store_release_gate(tmp_path, monkeypatch):
    paths, settings, manager = _setup(tmp_path)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        snap = get(client).json(); old = manager.runtime.current
        with monkeypatch.context() as patch:
            patch.setattr(manager.credential_store, "save", lambda state: (_ for _ in ()).throw(OSError("secret-failure")))
            failed = update(client, "tokens", snap["revision"], {"identity": "bob"}, "POST")
            assert failed.status_code == 500 and "secret-failure" not in failed.text
        assert manager.runtime.current is old and manager.revision() == snap["revision"]
        assert not manager.runtime._applying and not manager.credential_store.path.exists()
        request_token = manager.runtime.bind_request(operation=True)
        try:
            assert update(client, "tokens", snap["revision"], {"identity": "bob"}, "POST").status_code == 409
        finally:
            manager.runtime.reset_request(request_token); manager.runtime.end_operation()
        with monkeypatch.context() as patch:
            patch.setattr(manager.runtime.current.agent, "pending_approvals", lambda **kw: [object()])
            assert update(client, "tokens", snap["revision"], {"identity": "bob"}, "POST").status_code == 409
        with monkeypatch.context() as patch:
            patch.setattr(manager.credential_store, "read", lambda: (_ for _ in ()).throw(__import__("src.utils.admin_config", fromlist=["AdminConfigUnavailable"]).AdminConfigUnavailable("corrupt")))
            assert update(client, "tokens", snap["revision"], {"identity": "bob"}, "POST").status_code == 503
        assert not manager.runtime._applying


def test_credential_validation_errors_do_not_echo_request(tmp_path):
    paths, settings, manager = _setup(tmp_path)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        response = client.put("/admin/credentials/model", headers=_auth("admin-token"), json={"revision": "private-secret", "data": {"api_key": "secret-echo"}})
        assert response.status_code == 422
        assert "secret-echo" not in response.text and "private-secret" not in response.text


def test_registered_ssh_test_only_connects_and_invalidates_prior_tickets(tmp_path, monkeypatch, trusted_key):
    from src.utils import host_onboarding
    _, settings, manager = _setup(tmp_path)
    calls = []
    class SSH:
        def connect(self, **kw): calls.append(kw)
        def close(self): pass
        def exec_command(self, *a, **k): raise AssertionError("must not execute")
    monkeypatch.setattr(host_onboarding, "_new_client", lambda *a: SSH())
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        assert client.post("/admin/credentials/hosts/node-a/ssh/test", headers=_auth("user-token")).status_code == 403
        assert client.post("/admin/credentials/hosts/node-a/ssh/test", headers=_auth("admin-token")).status_code == 200
        service = manager._host_onboarding
        service._verified["old-ticket"] = ("old", 99999999)
        _, known = trusted_key
        result = update(client, "ssh/new-profile", get(client).json()["revision"], {"username": "u", "method": "password", "password": "page-pass", "known_hosts": known})
        assert result.status_code == 200 and not service._verified
        bound = update(client, "hosts/node-a/ssh", result.json()["revision"], {"credential_id": "new-profile"})
        assert bound.status_code == 200
        assert client.post("/admin/credentials/hosts/node-a/ssh/test", headers=_auth("admin-token")).status_code == 200
        assert calls[-1]["username"] == "u" and calls[-1]["password"] == "page-pass"


def test_model_factory_cache_is_shared_per_configuration_and_retains_old_instance(monkeypatch):
    import init
    from src.utils import model_loader
    import runpy
    from pathlib import Path
    # 独立加载缓存实现，保持 conftest 对应用模型入口的全局保护。
    get_model = runpy.run_path(str(Path(model_loader.__file__)))["get_model"]
    calls = []
    monkeypatch.setattr(init, "build_model", lambda config: calls.append(config) or object())
    first = get_model({"api_key": "isolated-key-a", "model": "a"})
    assert get_model({"model": "a", "api_key": "isolated-key-a"}) is first
    second = get_model({"api_key": "isolated-key-b", "model": "b"})
    assert second is not first and len(calls) == 2
    assert get_model({"api_key": "isolated-key-a", "model": "a"}) is first


def test_anonymous_credentials_remain_protected_when_business_auth_disabled(tmp_path):
    _, settings, manager = _setup(tmp_path, )
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        assert client.get("/hosts").status_code == 401
        assert client.get("/admin/credentials").status_code == 401
        assert client.get("/admin/credentials", headers=_auth("user-token")).status_code == 403
        snap = get(client).json()
        assert client.post("/admin/credentials/tokens", json={"revision": snap["revision"], "data": {"identity": "bob"}}).status_code == 401


def test_binding_failure_restores_hosts_store_and_runtime(tmp_path, monkeypatch, trusted_key):
    paths, settings, manager = _setup(tmp_path)
    _, known = trusted_key
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        saved = update(client, "ssh/rollback-key", get(client).json()["revision"], {"username": "u", "method": "password", "password": "isolated-password", "known_hosts": known})
        assert saved.status_code == 200
        before_hosts = paths["hosts"].read_bytes(); before_state = manager.credential_store.path.read_bytes(); before_runtime = manager.runtime.current
        monkeypatch.setattr(manager.credential_store, "save", lambda state: (_ for _ in ()).throw(OSError("offline persistence failure")))
        failed = update(client, "hosts/node-a/ssh", saved.json()["revision"], {"credential_id": "rollback-key"})
        assert failed.status_code == 500
        assert before_hosts == paths["hosts"].read_bytes() and before_state == manager.credential_store.path.read_bytes()
        assert manager.runtime.current is before_runtime and not manager.runtime._applying
        assert manager.revision() == saved.json()["revision"]


def test_concurrent_credential_writes_have_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    _, settings, manager = _setup(tmp_path)
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        revision = get(client).json()["revision"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda identity: update(client, "tokens", revision, {"identity": identity}, "POST"), ["bob", "carol"]))
        assert sorted(response.status_code for response in results) == [200, 409]
        assert len(manager.credential_store.read()["tokens"]) == 1


def test_ordinary_save_keeps_managed_secrets_out_of_yaml_and_backup(tmp_path, trusted_key):
    paths, settings, manager = _setup(tmp_path)
    _, known = trusted_key
    with TestClient(create_app(settings=settings, admin_config_manager=manager)) as client:
        saved = update(client, "ssh/managed-only", get(client).json()["revision"], {"username": "u", "method": "password", "password": "page-secret-private", "known_hosts": known})
        bound = update(client, "hosts/node-a/ssh", saved.json()["revision"], {"credential_id": "managed-only"})
        assert bound.status_code == 200
        draft = client.get("/admin/config", headers=_auth("admin-token")).json()
        draft["config"]["hosts"][0]["allow"]["rule_actions"] = []
        response = client.put("/admin/config", headers=_auth("admin-token"), json={"revision": draft["revision"], "config": draft["config"]})
        assert response.status_code == 200, response.text
        for file in [*paths.values(), *manager.backup_root.rglob("*.yaml")]:
            assert "page-secret-private" not in file.read_text(encoding="utf-8")
        assert manager.runtime.current.container.hosts().get("node-a").ssh.password == "page-secret-private"
