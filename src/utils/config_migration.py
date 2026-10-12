"""旧配置的一次性迁移和只读启动检查；正常运行没有旧字段回退。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path

MODEL_RENAMES = {
    "DEEPSEEK_API_KEY": "MODEL_API_KEY", "DEEPSEEK_BASE_URL": "MODEL_BASE_URL",
    "DEEPSEEK_MODEL": "MODEL_NAME", "DEEPSEEK_TIMEOUT": "MODEL_TIMEOUT",
    "DEEPSEEK_MAX_RETRIES": "MODEL_MAX_RETRIES", "DEEPSEEK_MAX_TOKENS": "MODEL_MAX_TOKENS",
}
SSH_KEYS = {"SSH_HOST", "SSH_PORT", "SSH_USER", "SSH_PASSWORD", "SSH_KEY_FILENAME", "SSH_KNOWN_HOSTS"}
OLD_REF = re.compile(r"\$\{(?:DEEPSEEK_|SSH_)[A-Za-z0-9_]*(?::-[^}]*)?\}")
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
JOURNAL = "assistant-data/config-migrations/current.json"


class ConfigMigrationError(ValueError):
    """只包含字段名和可操作说明，不含配置原文或密钥。"""


def reject_legacy_environment(values) -> None:
    keys = sorted(key for key in values if key in SSH_KEYS or key.startswith("DEEPSEEK_"))
    if keys:
        raise ConfigMigrationError("发现旧配置字段 " + ", ".join(keys) + "；请停止业务栈并运行 python scripts/stack.py migrate-config")


def reject_legacy_text(text: str) -> None:
    if OLD_REF.search(text):
        raise ConfigMigrationError("发现旧环境占位符；请先运行 migrate-config")


def reject_legacy_document(name: str, data: dict) -> None:
    if not isinstance(data, dict):
        raise ConfigMigrationError("运行配置必须是映射：" + name + ".yaml")
    if name == "settings":
        obsolete = (["server"] if "server" in data else []) + [
            section + "." + key for section, key in
            (("auth", "enabled"), ("alert", "trend_step"), ("app", "version"))
            if key in (data.get(section) or {})]
        if obsolete:
            raise ConfigMigrationError("发现已移除的配置字段 " + ", ".join(obsolete) + "；请先运行 migrate-config")
    if name in {"diagnostics", "agent_actions"} and "target" in data:
        raise ConfigMigrationError("旧全局 target 已移除；请先运行 migrate-config")
    if name == "settings" and (data.get("llm") or {}).get("provider") == "deepseek":
        raise ConfigMigrationError("旧模型供应商配置已移除；请先运行 migrate-config")


def assert_no_pending_migration(runtime: Path) -> None:
    path = runtime / JOURNAL
    if path.exists():
        try:
            pending = json.loads(path.read_text(encoding="utf-8")).get("status") != "complete"
        except (ValueError, AttributeError):
            pending = True
        if pending:
            raise ConfigMigrationError("配置迁移未完成；请保持服务停止并重新运行 migrate-config")


def check_runtime(runtime: Path) -> None:
    assert_no_pending_migration(runtime)
    source = runtime / "compose.env"
    if source.exists():
        reject_legacy_environment(_dotenv(source.read_text(encoding="utf-8")))
    for name in ("settings", "hosts", "metrics", "rules", "actions", "diagnostics", "agent_actions"):
        path = runtime / "config" / (name + ".yaml")
        if path.exists():
            from ruamel.yaml import YAML
            text = path.read_text(encoding="utf-8")
            reject_legacy_text(text)
            try:
                data = YAML(typ="safe").load(text) or {}
                reject_legacy_document(name, data)
            except ConfigMigrationError:
                raise
            except Exception:
                raise ConfigMigrationError("运行配置无法解析，请检查 " + name + ".yaml") from None


def _atomic(path: Path, content: bytes, mode: int = 0o600) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name == "posix":
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _json(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _dotenv(text: str) -> dict:
    from dotenv.parser import parse_stream
    from dotenv import dotenv_values
    bindings = list(parse_stream(io.StringIO(text)))
    if any(item.error for item in bindings):
        raise ConfigMigrationError("环境文件语法无效，请修正后迁移")
    keys = [item.key for item in bindings if item.key is not None]
    if len(keys) != len(set(keys)):
        raise ConfigMigrationError("环境文件有重复字段，请消除歧义后迁移")
    return {k: v for k, v in dotenv_values(stream=io.StringIO(text), interpolate=False).items() if v is not None}


def _rename(values: dict) -> dict:
    result = dict(values)
    for old, new in MODEL_RENAMES.items():
        if old in result:
            if new in result and result[new] != result[old]:
                raise ConfigMigrationError("新旧字段冲突：" + old + " / " + new)
            result[new] = result.pop(old)
    for key in list(result):
        if key in SSH_KEYS or key.startswith("SSH_"):
            del result[key]
    reject_legacy_environment(result)
    return result


def _env_text(original: str, values: dict) -> bytes:
    from dotenv.parser import parse_stream
    original_values = _dotenv(original)
    lines, written = [], set()
    for binding in parse_stream(io.StringIO(original)):
        key = binding.key
        if key is None:
            lines.append(binding.original.string)
            continue
        new = MODEL_RENAMES.get(key, key)
        if new not in values or new in written:
            continue
        written.add(new)
        if values[new] == original_values[key]:
            # 保留引号、注释和 Compose 对变量的原始解释方式。
            lines.append(re.sub(r"^(\s*(?:export\s+)?)" + re.escape(key) + r"(?=\s*=)", lambda m: m[1] + new, binding.original.string))
        else:
            lines.append(new + "=" + json.dumps(values[new], ensure_ascii=False) + "\n")
    for key in sorted(set(values) - written):
        lines.append(key + "=" + json.dumps(values[key], ensure_ascii=False) + "\n")
    return "".join(line if line.endswith("\n") else line + "\n" for line in lines).encode("utf-8")


def _resolve(value, values):
    if isinstance(value, str):
        return ENV_REF.sub(lambda m: values.get(m[1], m[2] or ""), value)
    if isinstance(value, dict):
        return {k: _resolve(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, values) for v in value]
    return value


def _material(path: str, runtime: Path, project: Path) -> str:
    if not path:
        raise ConfigMigrationError("旧 SSH 缺少可信 known_hosts 或私钥材料；请补充后迁移")
    source = Path(path)
    if path.startswith("/run/ssh/"):
        source = runtime / "ssh" / path[len("/run/ssh/"):]
    elif not source.is_absolute():
        source = project / source
    source = source.resolve()
    if not any(source.is_relative_to(root.resolve()) for root in (runtime, project)) or not source.is_file():
        raise ConfigMigrationError("SSH 材料不在可迁移目录内或文件缺失；请放入运行目录 ssh 后重试")
    try:
        return source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise ConfigMigrationError("SSH 材料无法读取；请修正权限或编码") from None


def _target(logical: str, runtime: Path, project: Path) -> Path:
    if logical == "@env":
        destination = project / ".env"
        if destination.is_symlink() or destination.resolve().parent != project.resolve():
            raise ConfigMigrationError("根环境文件不能是指向其他目录的链接")
        return destination
    allowed = {"compose.env", "deployment.json", "secrets/assistant.json", "alertmanager/token",
               "grafana/admin_user", "grafana/admin_password",
               "assistant-data/admin-credentials/state.json"}
    if logical not in allowed and logical not in {"config/" + name + ".yaml" for name in
            ("settings", "hosts", "metrics", "rules", "actions", "agent_actions", "diagnostics")}:
        raise ConfigMigrationError("迁移清单路径无效")
    destination = runtime / logical
    if not destination.resolve().is_relative_to(runtime.resolve()):
        raise ConfigMigrationError("迁移目标越出运行目录")
    return destination


def recover(runtime: Path, project: Path) -> None:
    """中断后先恢复原配置，再重新生成候选；不覆盖迁移外的人工修改。"""
    pointer = runtime / JOURNAL
    if not pointer.exists():
        return
    try:
        current = json.loads(pointer.read_text(encoding="utf-8"))
        if current["status"] == "complete":
            return
        identifier = current["id"]
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError
        directory = pointer.parent / identifier
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        restorations = []
        for item in manifest:
            if not re.fullmatch(r"[0-9]+", item["index"]) or not isinstance(item["existed"], bool):
                raise ValueError
            path = _target(item["path"], runtime, project)
            original = (directory / (item["index"] + ".before")).read_bytes() if item["existed"] else None
            staged = (directory / (item["index"] + ".after")).read_bytes()
            if hashlib.sha256(staged).hexdigest() != item["after_hash"] or (original is not None and hashlib.sha256(original).hexdigest() != item["before_hash"]):
                raise ValueError
            actual = path.read_bytes() if path.exists() else None
            if actual not in (original, staged):
                raise ConfigMigrationError("迁移中断后配置又被修改，请先核对受限备份：" + item["path"])
            restorations.append((path, original))
        for path, original in restorations:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, original)
        _atomic(pointer, _json({"id": identifier, "status": "complete", "rolled_back": True}))
    except ConfigMigrationError:
        raise
    except Exception:
        raise ConfigMigrationError("迁移恢复失败；保留备份和未完成标记，请检查目录权限与备份完整性") from None


def prepare(runtime: Path, project: Path) -> dict[str, bytes]:
    """只读生成完整候选，全部校验成功后才允许写入。"""
    from ruamel.yaml import YAML
    from src.utils.model_loader import model_config, _MODEL_ENV
    from src.utils.admin_credentials import CredentialStore, validate_ssh
    from src.utils.hosts_config import parse_hosts
    from src.plugins.diagnostics.commands import parse_diagnostics
    from src.plugins.agent_actions.catalog import parse_agent_actions
    from src.utils.config_loader import _build_settings, parse_yaml

    yaml = YAML()
    yaml.preserve_quotes = True
    env_path, source_path = project / ".env", runtime / "compose.env"
    env_original = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    source_original = source_path.read_text(encoding="utf-8")
    root_env, source_env = _dotenv(env_original), _dotenv(source_original)
    # 部署身份与服务密码只认持久来源；根 .env 从来不是这些字段的入口。
    protected = {"OPS_API_TOKENS", "OPS_ADMIN_IDENTITIES", "OPS_APPROVER_IDENTITIES",
                 "AIOPS_USER_TOKEN", "GF_SECURITY_ADMIN_USER", "GF_SECURITY_ADMIN_PASSWORD"}
    root_inputs = {key: value for key, value in root_env.items() if key not in protected}
    effective = {**source_env, **root_inputs}
    # 跨文件的新旧同义键同样不能猜测覆盖；同名字段仍保留原来源优先级。
    for old, new in MODEL_RENAMES.items():
        old_values = [values[old] for values in (root_env, source_env) if old in values]
        new_values = [values[new] for values in (root_env, source_env) if new in values]
        if any(old_value != new_value for old_value in old_values for new_value in new_values):
            raise ConfigMigrationError("新旧字段冲突：" + old + " / " + new)
    renamed_root, renamed_source = _rename(root_env), _rename(source_env)
    docs, originals = {}, {}
    for name in ("settings", "hosts", "metrics", "rules", "actions", "diagnostics", "agent_actions"):
        path = runtime / "config" / (name + ".yaml")
        originals[name] = path.read_text(encoding="utf-8")
        docs[name] = yaml.load(originals[name]) or {}
    store = CredentialStore(runtime / "assistant-data" / "admin-credentials")
    state = store.read()
    old_provider = (docs["settings"].get("llm") or {}).get("provider") == "deepseek"
    if old_provider:
        docs["settings"]["llm"]["provider"] = "openai_compatible"
        # 原供应商的隐式默认值必须显式保留，不能让通用 SDK 换成别的端点。
        renamed_source.setdefault("MODEL_BASE_URL", "https://api.deepseek.com")
        renamed_source.setdefault("MODEL_NAME", "deepseek-v4-flash")
        if not renamed_source["MODEL_BASE_URL"]:
            renamed_source["MODEL_BASE_URL"] = "https://api.deepseek.com"
        if not renamed_source["MODEL_NAME"]:
            renamed_source["MODEL_NAME"] = "deepseek-v4-flash"
        if state["model"] is not None and not state["model"].get("base_url"):
            state["model"]["base_url"] = "https://api.deepseek.com"
    if state["model"] is not None:
        state["model"] = model_config(state["model"])
    from scripts.deployment import validate_credentials
    validate_credentials(renamed_source)
    if not renamed_source.get("GF_SECURITY_ADMIN_USER"):
        raise ConfigMigrationError("部署凭据缺少 Grafana 用户；请恢复持久来源")
    config_env = {**renamed_source, **{key: value for key, value in renamed_root.items() if key not in protected}}
    env_model = model_config({key: config_env.get(env, default) for key, (env, default) in
                            _MODEL_ENV.items()})
    if env_model["api_key"] and not all(env_model[k] for k in ("model", "base_url")):
        raise ConfigMigrationError("环境模型配置不完整，请补充 MODEL_BASE_URL 和 MODEL_NAME")

    credentials = docs["hosts"].get("credentials") or {}
    for reference, spec in list(credentials.items()):
        if not OLD_REF.search(json.dumps(spec)) and not any(str(spec.get(k, "")).startswith("/run/ssh/") for k in ("key_filename", "known_hosts")):
            continue
        resolved = _resolve(spec, effective)
        managed_id = "migrated-" + hashlib.sha256(str(reference).encode()).hexdigest()[:16]
        if resolved.get("key_filename") and resolved.get("password"):
            raise ConfigMigrationError("旧 SSH 同时配置密码与私钥；请明确认证方式后迁移")
        private = _material(resolved.get("key_filename", ""), runtime, project) if resolved.get("key_filename") else ""
        candidate = validate_ssh({
            "username": resolved.get("username", "root"),
            "method": "private_key" if private else "password",
            **({"private_key": private} if private else {"password": resolved.get("password", "")}),
            "known_hosts": _material(resolved.get("known_hosts", ""), runtime, project),
        }, None)
        if managed_id in state["ssh"] and state["ssh"][managed_id] != candidate:
            raise ConfigMigrationError("迁移 SSH 凭据 ID 冲突，请人工处理")
        state["ssh"][managed_id] = candidate
        for host in docs["hosts"].get("hosts", []):
            if (host.get("ssh") or {}).get("credential_ref") == reference:
                host["ssh"]["credential_ref"] = managed_id
        del credentials[reference]
    # 页面存储也参与整体校验；损坏的材料不能随迁移发布。
    for key, spec in state["ssh"].items():
        if bool(spec["private_key"]) == bool(spec["password"]):
            raise ConfigMigrationError("页面 SSH 凭据认证材料无效")
        state["ssh"][key] = validate_ssh({
            "username": spec["username"], "known_hosts": spec["known_hosts"],
            "method": "private_key" if spec["private_key"] else "password",
            **({"private_key": spec["private_key"]} if spec["private_key"] else {"password": spec["password"]}),
        }, None)
    docs["hosts"]["credentials"] = credentials
    for host in docs["hosts"].get("hosts", []):
        if host.get("ssh"):
            host["ssh"] = _resolve(host["ssh"], effective)
    settings_hosts = docs["settings"].get("hosts") or {}
    if not settings_hosts.get("file"):
        # 不推断旧单主机属于哪个现有节点，也不复制隐式的全命令权限。
        if any(docs[name].get("enabled") for name in ("diagnostics", "agent_actions")):
            raise ConfigMigrationError("旧单主机缺少明确登记及授权；请先补充 hosts.file 和主机清单")
        docs["settings"]["hosts"] = {"file": "config/hosts.yaml"}
    for name in ("diagnostics", "agent_actions"):
        docs[name].pop("target", None)
    docs["settings"].pop("server", None)
    for section, key in (("auth", "enabled"), ("alert", "trend_step"), ("app", "version")):
        (docs["settings"].get(section) or {}).pop(key, None)

    # 旧工具写回 YAML 可能去掉引号；按字段类型恢复字符串占位符语义。
    from dataclasses import fields, is_dataclass
    from typing import get_type_hints
    from ruamel.yaml.scalarstring import DoubleQuotedScalarString
    from src.utils.config_loader import Settings
    def quote_strings(doc, schema):
        hints = get_type_hints(schema)
        for field in fields(schema):
            value = doc.get(field.name)
            typ = hints[field.name]
            if is_dataclass(typ) and isinstance(value, dict):
                quote_strings(value, typ)
            elif typ is str and isinstance(value, str) and ENV_REF.search(value):
                doc[field.name] = DoubleQuotedScalarString(value)
    quote_strings(docs["settings"], Settings)
    candidates = {}
    for name, doc in docs.items():
        stream = io.StringIO()
        yaml.dump(doc, stream)
        text = stream.getvalue()
        reject_legacy_text(text)
        reject_legacy_document(name, doc)
        candidates["config/" + name + ".yaml"] = text.encode("utf-8")
    # 校验时只临时设置候选环境，不改变调用方环境。
    previous = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update({**previous, **config_env})
        for key in list(os.environ):
            if key in SSH_KEYS or key.startswith("DEEPSEEK_"):
                del os.environ[key]
        settings = _build_settings(parse_yaml(candidates["config/settings.yaml"].decode()))
        overrides = {}
        for key, spec in state["ssh"].items():
            overrides[key] = {"username": spec["username"], "password": spec.get("password") or None,
                              "key_filename": "managed-private-key" if spec.get("private_key") else None,
                              "known_hosts": "managed-known-hosts"}
        hosts = parse_hosts(candidates["config/hosts.yaml"].decode(), credential_overrides=overrides)
        diag = parse_diagnostics(candidates["config/diagnostics.yaml"].decode())
        actions = parse_agent_actions(candidates["config/agent_actions.yaml"].decode())
        from src.plugins.actions.catalog import parse_actions
        from src.utils.metrics_config import parse_metrics_config
        from src.storage.models import RulesFile
        from src.utils.auth import ensure_usable, parse_tokens, parse_admins, parse_approvers
        from src.utils.host_access import parse_host_access
        rule_actions = parse_actions(candidates["config/actions.yaml"].decode())
        parse_metrics_config(candidates["config/metrics.yaml"].decode())
        rules = RulesFile(**parse_yaml(candidates["config/rules.yaml"].decode()))
        settings = CredentialStore.settings(settings, state)
        ensure_usable(settings.auth.tokens, approval_mode=settings.auth.approval_mode, approvers_raw=settings.auth.approver_identities)
        identities = {identity for _, identity in parse_tokens(settings.auth.tokens)}
        admins = set(parse_admins(settings.auth.admin_identities))
        if not admins or not admins <= identities:
            raise ConfigMigrationError("管理员身份必须全部存在于 Token 映射")
        parse_approvers(settings.auth.approver_identities)
        known = {host.host_id for host in hosts.all()}
        if any(set(ids) - known for ids in parse_host_access(settings.auth.host_access).values()):
            raise ConfigMigrationError("主机授权引用了未知主机")
        catalogs = {"diagnostics": set(docs["diagnostics"].get("commands") or {}), "agent_actions": set(docs["agent_actions"].get("commands") or {}), "rule_actions": set(docs["actions"].get("commands") or {})}
        for host in hosts.all():
            for name, commands in catalogs.items():
                if set(getattr(host.capabilities, name)) - commands:
                    raise ConfigMigrationError("主机授权引用了未知命令")
        from src.plugins.common.cmddef import parse_command_def, validated_params
        from src.plugins.common.endpoint import CONNECTION_FIELDS
        action_defs = {key: parse_command_def(key, spec, allow_sudo=True) for key, spec in (docs["actions"].get("commands") or {}).items()}
        for rule in rules.rules:
            if rule.action not in action_defs:
                raise ConfigMigrationError("告警规则引用了未知动作")
            validated_params(action_defs[rule.action], {key: value for key, value in rule.params.items() if key not in CONNECTION_FIELDS})
            if rule.auto_execute and not rule.target_instance:
                raise ConfigMigrationError("自动执行规则缺少明确目标")
    finally:
        os.environ.clear()
        os.environ.update(previous)
    candidates["compose.env"] = _env_text(source_original, renamed_source)
    if env_path.exists():
        candidates["@env"] = _env_text(env_original, renamed_root)
    if store.path.exists() or state["ssh"] or state["model"] is not None:
        candidates["assistant-data/admin-credentials/state.json"] = _json(state)
    values = {key: value for key, value in renamed_source.items() if key.startswith("MODEL_") or key in
              {"OPS_REDACTION_SECRETS", "OPS_API_TOKENS", "OPS_ADMIN_IDENTITIES", "OPS_APPROVER_IDENTITIES"}}
    candidates["secrets/assistant.json"] = _json(values)
    metadata_path = runtime / "deployment.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if metadata.get("version") not in {1, 2} or metadata.get("config_source") not in {"bundled", "legacy", "container"}:
        raise ConfigMigrationError("部署元数据无效；请恢复初始化状态文件")
    metadata["configuration_version"] = 1
    candidates["deployment.json"] = _json(metadata)
    return {logical: content for logical, content in candidates.items()
            if not _target(logical, runtime, project).exists() or _target(logical, runtime, project).read_bytes() != content}


def migrate(runtime: Path, project: Path, *, dry_run: bool = False) -> list[str]:
    """先验证并备份全部文件，再发布；普通异常回滚，中断由下次命令恢复。"""
    if dry_run:
        assert_no_pending_migration(runtime)
    else:
        recover(runtime, project)
    try:
        candidates = prepare(runtime, project)
    except ConfigMigrationError:
        raise
    except Exception:
        raise ConfigMigrationError("迁移校验失败；没有发布改动，请检查配置结构、凭据及文件权限") from None
    if dry_run or not candidates:
        return sorted(candidates)
    import uuid
    identifier = uuid.uuid4().hex
    pointer = runtime / JOURNAL
    directory = pointer.parent / identifier
    manifest = []
    # 写备份失败时尚未触碰任何源文件。
    for index, (logical, content) in enumerate(candidates.items()):
        path = _target(logical, runtime, project)
        original = path.read_bytes() if path.exists() else None
        item = {"path": logical, "index": str(index), "existed": original is not None,
                "before_hash": hashlib.sha256(original).hexdigest() if original is not None else None,
                "after_hash": hashlib.sha256(content).hexdigest()}
        if original is not None:
            _atomic(directory / (str(index) + ".before"), original)
        _atomic(directory / (str(index) + ".after"), content)
        manifest.append(item)
    _atomic(directory / "manifest.json", _json(manifest))
    _atomic(pointer, _json({"id": identifier, "status": "pending"}))
    try:
        for logical, content in candidates.items():
            _atomic(_target(logical, runtime, project), content)
        _atomic(pointer, _json({"id": identifier, "status": "complete"}))
    except Exception:
        recover(runtime, project)
        raise ConfigMigrationError("迁移写入失败，已恢复原配置；请检查权限后重试") from None
    return sorted(candidates)
