"""仅供隔离 Compose：构造旧配置并核验显式迁移，禁止用于业务部署。"""
from __future__ import annotations
import hashlib
import json
import sys
from pathlib import Path

RUNTIME = Path("/runtime")
PROJECT = Path("/migration-project")
STATE = RUNTIME / "assistant-data/migration-smoke.json"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    from ruamel.yaml import YAML
    from src.utils.config_migration import MODEL_RENAMES, check_runtime, _dotenv
    from src.utils.admin_credentials import CredentialStore
    yaml = YAML()
    yaml.preserve_quotes = True
    if "--seed" in sys.argv:
        assert not STATE.exists(), "隔离迁移夹具已存在"
        settings = RUNTIME / "config/settings.yaml"
        doc = yaml.load(settings.read_text())
        doc["llm"]["provider"] = "deepseek"
        doc["auth"]["enabled"] = False
        doc["app"]["version"] = "old"
        doc["server"] = {"port": 9000}
        with settings.open("w") as out:
            yaml.dump(doc, out)
        for name in ("diagnostics", "agent_actions"):
            with (RUNTIME / "config" / (name + ".yaml")).open("a") as out:
                out.write("\ntarget: {host: unused-isolated-target}\n")
        source = RUNTIME / "compose.env"
        text = source.read_text()
        for old, new in MODEL_RENAMES.items():
            text = text.replace(new + "=", old + "=")
        source.write_text(text)
        PROJECT.mkdir(parents=True, exist_ok=True)
        (PROJECT / ".env").write_text('DEEPSEEK_MODEL="synthetic-migrated-model"\n')
        credentials = CredentialStore(RUNTIME / "assistant-data/admin-credentials")
        state = credentials.read()
        assert state["model"] is None, "迁移夹具要求新隔离栈"
        state["model"] = {"api_key": "", "base_url": "https://isolated.invalid/v1",
                          "model": "page-model", "timeout": 12, "max_retries": 0, "max_tokens": 64}
        state["tokens"]["migration-fixture"] = {"identity": "migration-fixture", "secret": "isolated-migration-fixture-token"}
        credentials.save(state)
        values = _dotenv(source.read_text())
        STATE.write_text(json.dumps({
            "preserved": {key: value for key, value in values.items() if not key.startswith("DEEPSEEK_")},
            "service_files": {name: digest(RUNTIME / name) for name in
                              ("alertmanager/token", "grafana/admin_user", "grafana/admin_password")},
            "hosts": digest(RUNTIME / "config/hosts.yaml"),
        }))
        STATE.chmod(0o600)
        print("PASS: 已构造隔离旧配置与已有页面配置")
    else:
        from scripts.deployment import read_credentials
        saved = json.loads(STATE.read_text())
        check_runtime(RUNTIME)
        values = read_credentials(RUNTIME)
        assert all(values[key] == value for key, value in saved["preserved"].items())
        assert all(digest(RUNTIME / name) == value for name, value in saved["service_files"].items())
        if digest(RUNTIME / "config/hosts.yaml") != saved["hosts"]:
            # YAML 序列缩进可能规范化，语义、主机关联和授权必须完全一致。
            pointer = RUNTIME / "assistant-data/config-migrations/current.json"
            transaction = pointer.parent / json.loads(pointer.read_text())["id"]
            manifest = json.loads((transaction / "manifest.json").read_text())
            entry = next(item for item in manifest if item["path"] == "config/hosts.yaml")
            backup = transaction / (entry["index"] + ".before")
            assert digest(backup) == saved["hosts"]
            assert yaml.load(backup.read_text()) == yaml.load((RUNTIME / "config/hosts.yaml").read_text())
        state = CredentialStore(RUNTIME / "assistant-data/admin-credentials").read()
        assert state["model"]["model"] == "page-model" and state["model"]["api_key"] == ""
        assert state["model"]["token_limit_param"] == "max_tokens"
        assert state["tokens"]["migration-fixture"]["secret"] == "isolated-migration-fixture-token"
        assert _dotenv((PROJECT / ".env").read_text()) == {"MODEL_NAME": "synthetic-migrated-model"}
        print("PASS: 原部署身份、授权、服务凭据及页面模型保留；旧字段已迁移")


if __name__ == "__main__":
    main()
