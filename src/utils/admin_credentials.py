"""管理员凭据：独立持久化、脱敏元信息与配置快照事务。

页面凭据不写回 YAML，旧部署凭据只读保留。私钥资源按内容版本保存，旧请求
仍可使用旧快照的路径；运行目录属于部署备份的一部分，不属于普通配置备份。
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import os
import re
import secrets
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from src.utils.admin_config import AdminConfigConflict, AdminConfigError, AdminConfigUnavailable
from src.utils.auth import parse_tokens
from src.utils.config_loader import parse_yaml
from src.utils.security import register_secrets

_ID = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")


def atomic_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    fd, name = tempfile.mkstemp(prefix=".credential-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class CredentialStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "state.json"

    def read(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "tokens": {}, "ssh": {}, "model": None}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("version") != 1
                    or set(value) != {"version", "tokens", "ssh", "model"}
                    or not isinstance(value["tokens"], dict) or not isinstance(value["ssh"], dict)
                    or value["model"] is not None and not isinstance(value["model"], dict)):
                raise ValueError
            for entry in value["tokens"].values():
                if (not isinstance(entry, dict) or not isinstance(entry.get("secret"), str) or not entry.get("secret")
                        or not isinstance(entry.get("identity"), str) or not entry.get("identity")):
                    raise ValueError
            for key, entry in value["ssh"].items():
                if (not _ID.fullmatch(key) or not isinstance(entry, dict)
                        or not isinstance(entry.get("username"), str) or not entry.get("username")
                        or not isinstance(entry.get("known_hosts"), str) or not entry.get("known_hosts")
                        or not all(isinstance(entry.get(name), str) for name in ("password", "private_key"))):
                    raise ValueError
            register_secrets(*[entry["secret"] for entry in value["tokens"].values()])
            register_secrets(*[entry.get("password", "") for entry in value["ssh"].values()])
            if value["model"] is not None:
                validate_model(value["model"])
                register_secrets(*[value["model"].get("api_key", "")])
            return value
        except (ValueError, TypeError, KeyError, OSError, AdminConfigError):
            raise AdminConfigUnavailable("管理员凭据存储无法读取，请恢复部署备份") from None

    def save(self, state: dict) -> None:
        atomic_private(self.path, json.dumps(state, ensure_ascii=False, sort_keys=True))

    def ssh_specs(self, state: dict) -> dict:
        specs = {}
        for key, entry in state["ssh"].items():
            material = entry.get("private_key", "") + "\0" + entry["known_hosts"]
            asset = self.directory / "assets" / hashlib.sha256(material.encode()).hexdigest()
            known = asset / "known_hosts"
            private = asset / "private_key"
            # 内容版本目录不可覆盖。首次写入/恢复时创建，旧快照保留旧路径。
            if not known.exists():
                atomic_private(known, entry["known_hosts"])
            if entry.get("private_key") and not private.exists():
                atomic_private(private, entry["private_key"])
            specs[key] = {"username": entry["username"], "password": entry.get("password") or None,
                          "key_filename": str(private) if entry.get("private_key") else None,
                          "known_hosts": str(known)}
        return specs

    @staticmethod
    def model_config(state: dict) -> dict:
        from src.utils.model_loader import model_config
        return model_config(state["model"])

    @staticmethod
    def settings(settings, state: dict):
        if not state["tokens"]:
            return settings
        tokens = dict(parse_tokens(settings.auth.tokens))
        for entry in state["tokens"].values():
            if entry["secret"] in tokens:
                raise AdminConfigError("新增 Token 与部署凭据冲突")
            tokens[entry["secret"]] = entry["identity"]
        return replace(settings, auth=replace(settings.auth, tokens=json.dumps(tokens, ensure_ascii=False)))


def validate_model(data: dict) -> dict:
    from src.utils.model_loader import model_config
    try:
        return model_config(data)
    except ValueError as exc:
        raise AdminConfigError(str(exc)) from None


def validate_ssh(data: dict, existing: dict | None) -> dict:
    if set(data) - {"username", "method", "password", "private_key", "known_hosts"}:
        raise AdminConfigError("SSH 凭据包含未知字段")
    username = data.get("username")
    if not isinstance(username, str) or not username.strip() or len(username) > 128 or any(ord(c) < 32 for c in username):
        raise AdminConfigError("SSH 用户名无效")
    known = data.get("known_hosts", (existing or {}).get("known_hosts", ""))
    if not isinstance(known, str) or not known.strip() or len(known) > 131072:
        raise AdminConfigError("请录入可信 known_hosts，最长 128 KiB")
    try:
        import paramiko
        lines = [line for line in known.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if not lines or any((entry := paramiko.hostkeys.HostKeyEntry.from_line(line)) is None or entry.key is None for line in lines):
            raise ValueError
    except Exception:
        raise AdminConfigError("known_hosts 格式无效；请使用已核验的主机公钥") from None
    method = data.get("method", "password")
    result = {"username": username.strip(), "known_hosts": known, "password": "", "private_key": ""}
    if method == "password":
        password = data.get("password", (existing or {}).get("password", ""))
        if not isinstance(password, str) or not password or len(password) > 8192:
            raise AdminConfigError("SSH 密码不能为空且最长 8192 字符")
        result["password"] = password
        register_secrets(*[password])
    elif method == "private_key":
        private = data.get("private_key", (existing or {}).get("private_key", ""))
        if not isinstance(private, str) or len(private) > 131072 or not private:
            raise AdminConfigError("私钥不能为空且最长 128 KiB")
        try:
            loaded = False
            for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                try:
                    cls.from_private_key(io.StringIO(private))
                    loaded = True
                    break
                except paramiko.PasswordRequiredException:
                    raise AdminConfigError("暂不支持带口令的私钥") from None
                except (paramiko.SSHException, ValueError):
                    pass
            if not loaded:
                raise AdminConfigError("私钥无效或不支持该格式")
        except AdminConfigError:
            raise
        except Exception:
            raise AdminConfigError("私钥解析失败") from None
        result["private_key"] = private
        register_secrets(*[private])
    else:
        raise AdminConfigError("SSH 认证方式必须是 password 或 private_key")
    return result


class AdminCredentials:
    def __init__(self, manager):
        self.manager = manager

    def snapshot(self):
        with self.manager._lock:
            manager = self.manager
            state = manager.credential_store.read()
            settings = manager.runtime.current.settings
            managed = {entry["secret"] for entry in state["tokens"].values()}
            base = [{"id": "deployment-" + hashlib.sha256(token.encode()).hexdigest()[:16],
                     "identity": identity, "managed": False} for token, identity in parse_tokens(settings.auth.tokens)
                    if token not in managed]
            current = CredentialStore.model_config(state)
            return {"revision": manager.revision(), "tokens": base + [
                        {"id": key, "identity": item["identity"], "managed": True}
                        for key, item in state["tokens"].items()],
                    "ssh": [{"id": key, "username": str(item.get("username", "")), "managed": False,
                             "method": "private_key" if item.get("key_filename") else "password"}
                            for key, item in (manager._load_docs()["hosts"].get("credentials") or {}).items()] +
                           [{"id": key, "username": item["username"], "managed": True,
                             "method": "private_key" if item.get("private_key") else "password"}
                            for key, item in state["ssh"].items()],
                    "model": {key: value for key, value in current.items() if key != "api_key"} | {
                        "configured": bool(current.get("api_key")), "managed": state["model"] is not None}}

    def change(self, revision, kind, key, action, data, actor):
        manager = self.manager
        with manager._lock:
            if revision != manager.revision():
                raise AdminConfigConflict("配置已变化，请重新加载后保存")
            manager.runtime.begin_apply()
            try:
                old_bytes = manager.credential_store.path.read_bytes() if manager.credential_store.path.exists() else None
                docs = manager._load_docs()
                old_hosts = manager.paths["hosts"].read_bytes().decode("utf-8")
                state = copy.deepcopy(manager.credential_store.read())
            except Exception:
                manager.runtime.cancel_apply()
                raise
            token_once = None
            try:
                if kind == "tokens":
                    if set(data) - ({"identity"} if action == "create" else set()):
                        raise AdminConfigError("Token 操作包含未知字段")
                    if action == "create":
                        identity = data.get("identity")
                        if not isinstance(identity, str) or not identity.strip() or len(identity) > 128 or any(ord(c) < 32 for c in identity):
                            raise AdminConfigError("身份名无效")
                        identity = identity.strip()
                        if identity == manager.runtime.current.settings.auth.alert_identity:
                            raise AdminConfigError("Webhook 身份由部署凭据管理，不能在页面创建")
                        key = "token-" + secrets.token_hex(16)
                        state["tokens"][key] = {"identity": identity, "secret": secrets.token_urlsafe(32)}
                        token_once = state["tokens"][key]["secret"]
                    elif key not in state["tokens"]:
                        raise AdminConfigError("只能管理页面创建的 Token")
                    elif action == "rotate":
                        state["tokens"][key]["secret"] = secrets.token_urlsafe(32)
                        token_once = state["tokens"][key]["secret"]
                    elif action == "delete":
                        del state["tokens"][key]
                    else:
                        raise AdminConfigError("Token 操作无效")
                elif kind == "ssh":
                    if not _ID.fullmatch(key):
                        raise AdminConfigError("凭据 ID 须为小写字母开头的字母、数字、下划线或连字符")
                    inherited = (docs["hosts"].get("credentials") or {})
                    if key in inherited:
                        raise AdminConfigError("部署文件中的 SSH 凭据只读，请使用新的凭据 ID")
                    if action == "delete":
                        if any((h.get("ssh") or {}).get("credential_ref") == key for h in docs["hosts"]["hosts"]):
                            raise AdminConfigConflict("该凭据仍被主机引用，请先更改关联")
                        if key not in state["ssh"]:
                            raise AdminConfigError("未知 SSH 凭据")
                        del state["ssh"][key]
                    else:
                        state["ssh"][key] = validate_ssh(data, state["ssh"].get(key))
                elif kind == "model":
                    current = CredentialStore.model_config(state)
                    update = dict(data)
                    mode = update.pop("key_action", "keep")
                    if mode == "keep":
                        update["api_key"] = current["api_key"]
                    elif mode == "clear":
                        update["api_key"] = ""
                    elif mode == "replace":
                        if not isinstance(update.get("api_key"), str) or not update["api_key"].strip():
                            raise AdminConfigError("替换密钥时请输入新 API Key；清除请使用 clear")
                    else:
                        raise AdminConfigError("密钥操作必须为 keep、replace 或 clear")
                    state["model"] = validate_model({**current, **update})
                    if state["model"]["api_key"]:
                        from src.utils.model_loader import model_config
                        try:
                            model_config(state["model"], require_complete=True)
                        except ValueError as exc:
                            raise AdminConfigError(str(exc)) from None
                    register_secrets(*[state["model"]["api_key"]])
                elif kind == "host":
                    if set(data) != {"credential_id"}:
                        raise AdminConfigError("主机关联只接受凭据 ID")
                    host = next((h for h in docs["hosts"]["hosts"] if h["id"] == key), None)
                    if host is None:
                        raise AdminConfigError("未知主机")
                    cred = data.get("credential_id", "")
                    if cred not in manager.credential_specs(docs, state):
                        raise AdminConfigError("请选择有效的 SSH 凭据")
                    if not host.get("ssh"):
                        from src.utils.host_onboarding import HostOnboarding, _address_host
                        targets = HostOnboarding(manager)._targets()
                        matches = [t for t in targets if t["job"] == host["prometheus"]["job"]
                                   and t["instance"] == host["prometheus"]["instance"] and t["ssh_eligible"]]
                        if len(matches) != 1:
                            raise AdminConfigError("当前采集目标不能安全推导 SSH 地址")
                        host["ssh"] = {"host": _address_host(matches[0]["address"]), "port": 22}
                    host["ssh"]["credential_ref"] = cred
                else:
                    raise AdminConfigError("未知凭据类别")
                manager.credential_store.ssh_specs(state)
                manager._validate(docs, credential_state=state)
                candidate = manager._candidate(docs, credential_state=state)
                if manager.revision() != revision:
                    raise AdminConfigConflict("校验期间配置已变化")
                host_text = manager._dump(docs["hosts"])
                host_changed = kind == "host" and host_text != old_hosts
                if host_changed:
                    backup = manager.backup_root / ("credential-host-" + secrets.token_hex(12))
                    backup.mkdir(parents=True)
                    (backup / "hosts.yaml").write_bytes(old_hosts.encode("utf-8"))
                    manager._atomic_write(manager.paths["hosts"], host_text)
                manager.credential_store.save(state)
                new_revision = manager.revision()
                manager.runtime.commit(replace(candidate, revision=new_revision))
                manager.active_revision = new_revision
                service = getattr(manager, "_host_onboarding", None)
                if service:
                    service._verified.clear()
                if token_once:
                    register_secrets(*[token_once])
            except Exception:
                try:
                    if manager.paths["hosts"].read_bytes().decode("utf-8") != old_hosts:
                        manager._atomic_write(manager.paths["hosts"], old_hosts)
                    if old_bytes is None:
                        manager.credential_store.path.unlink(missing_ok=True)
                    else:
                        atomic_private(manager.credential_store.path, old_bytes.decode("utf-8"))
                finally:
                    manager.runtime.cancel_apply()
                raise
            import logging
            logging.getLogger("aiops_assistant.audit").info(
                "credential_change actor=%s kind=%s id=%s action=%s", actor, kind, key, action)
            result = self.snapshot()
            if token_once:
                result["token_once"] = token_once
                result["token_id"] = key
            return result
