"""管理员结构化配置读写：脱敏快照、完整校验、备份与原子替换。

这里刻意不提供原始 YAML 编辑器。浏览器只收到可编辑的安全投影；连接目标、凭据、
Token 明文和环境变量表达式留在服务端原始文档中。保存时以原文为底稿合并安全字段，
再交给现有解析器和跨文件引用校验，避免前端草稿成为新的配置真相来源。

revision 是磁盘内容的 SHA-256；active_revision 是当前运行快照的版本。
每个文件用同目录临时文件 + os.replace 写入；批次不是跨文件的
原子事务，Python 异常时从备份回滚，进程崩溃或断电仍需要人工核对磁盘与备份。
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import shutil
import tempfile
import threading
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.plugins.actions.catalog import DEFAULT_ACTIONS_FILE, parse_actions
from src.plugins.agent_actions.catalog import DEFAULT_AGENT_ACTIONS_FILE, parse_agent_actions
from src.plugins.common.cmddef import parse_command_def, validated_params
from src.plugins.common.endpoint import CONNECTION_FIELDS
from src.plugins.diagnostics.commands import DEFAULT_DIAGNOSTICS_FILE, parse_diagnostics
from src.storage.models import RulesFile
from src.utils.auth import parse_admins, parse_approvers, parse_tokens
from src.utils.config_loader import (
    DEFAULT_SETTINGS_FILE,
    ROOT_DIR,
    Settings,
    _build_settings,
    interpolate,
)
from src.utils.hosts_config import parse_hosts
from src.utils.metrics_config import parse_metrics_config
from src.utils.runtime import RuntimeBusyError, RuntimeSnapshot

logger = logging.getLogger("aiops_assistant.admin_config")

DEFAULT_RULES_FILE = ROOT_DIR / "config" / "rules.yaml"
_RULE_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
_CATALOGS = {
    "rule_actions": ("actions", True),
    "agent_actions": ("agent_actions", True),
    "diagnostics": ("diagnostics", False),
}


class AdminConfigError(ValueError):
    """浏览器提交的配置无效。"""


class AdminConfigConflict(AdminConfigError):
    """磁盘配置已被其它请求修改。"""


class AdminConfigUnavailable(RuntimeError):
    """配置文件或 round-trip YAML 能力不可用。"""


def _path(value: str | Path | None, fallback: Path) -> Path:
    result = Path(value) if value else fallback
    return result if result.is_absolute() else ROOT_DIR / result


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return path.name


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _sync_mapping(target: Any, values: Mapping[str, Any]) -> None:
    """就地同步映射，保留 ruamel 附着在既有键上的注释与样式。"""
    for key in list(target):
        if key not in values:
            del target[key]
    for key, value in values.items():
        if key not in target or _plain(target[key]) != _plain(value):
            target[key] = value


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AdminConfigError(f"{what} 必须是对象")
    return dict(value)


def _list(value: Any, what: str) -> list[Any]:
    if not isinstance(value, list):
        raise AdminConfigError(f"{what} 必须是数组")
    return value


def _bool(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise AdminConfigError(f"{what} 必须是布尔值")
    return value


def _text(value: Any, what: str, *, required: bool = False, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise AdminConfigError(f"{what} 必须是字符串")
    result = value.strip()
    if required and not result:
        raise AdminConfigError(f"{what} 不能为空")
    if len(result) > maximum:
        raise AdminConfigError(f"{what} 不能超过 {maximum} 个字符")
    return result


class AdminConfigManager:
    """管理六个基础 YAML、可选指标/目标文件；浏览器只能编辑受限投影。"""

    def __init__(
        self,
        settings: Settings,
        *,
        settings_file: str | Path | None = None,
        hosts_file: str | Path | None = None,
        rules_file: str | Path | None = None,
        actions_file: str | Path | None = None,
        agent_actions_file: str | Path | None = None,
        diagnostics_file: str | Path | None = None,
        metrics_file: str | Path | None = None,
        backup_root: str | Path | None = None,
    ) -> None:
        self.paths = {
            "settings": _path(settings_file or os.getenv("OPS_SETTINGS_FILE"), DEFAULT_SETTINGS_FILE),
            "hosts": _path(hosts_file or settings.hosts.file, ROOT_DIR / "config" / "hosts.yaml"),
            "rules": _path(rules_file or os.getenv("OPS_RULES_FILE"), DEFAULT_RULES_FILE),
            "actions": _path(actions_file or os.getenv("OPS_ACTIONS_FILE"), DEFAULT_ACTIONS_FILE),
            "agent_actions": _path(
                agent_actions_file or os.getenv("OPS_AGENT_ACTIONS_FILE"),
                DEFAULT_AGENT_ACTIONS_FILE,
            ),
            "diagnostics": _path(
                diagnostics_file or os.getenv("OPS_DIAGNOSTICS_FILE"),
                DEFAULT_DIAGNOSTICS_FILE,
            ),
        }
        if metrics_file or settings.metrics.file:
            self.paths["metrics"] = _path(
                metrics_file or settings.metrics.file, ROOT_DIR / "config" / "metrics.yaml"
            )
        self.target_path = (
            _path(settings.prometheus.file_sd_path, ROOT_DIR / "unused")
            if settings.prometheus.target_mode == "managed" else None
        )
        if self.target_path is not None and self.target_path.resolve() in {
            path.resolve() for path in self.paths.values()
        }:
            raise AdminConfigUnavailable("Prometheus 目标文件不能覆盖应用配置文件")
        self.backup_root = _path(backup_root, ROOT_DIR / "data" / "config-backups")
        self._lock = threading.Lock()
        # attach_runtime 前只知道磁盘版本；运行版本以创建的第一份快照为准。
        self.active_revision = self.revision()
        self.runtime = None

    def attach_runtime(self, runtime: Any) -> None:
        self.runtime = runtime
        self.active_revision = runtime.current.revision

    @staticmethod
    def _yaml():
        try:
            from ruamel.yaml import YAML
        except ImportError as exc:
            raise AdminConfigUnavailable(
                "缺少 ruamel.yaml，无法安全保留配置注释与格式"
            ) from exc
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=4, offset=2)
        return yaml

    def revision(self) -> str:
        """计算所有受管理文件的磁盘指纹；环境变量值不参与此指纹。"""
        digest = hashlib.sha256()
        for name, path in sorted(self.paths.items()):
            digest.update(name.encode("utf-8") + b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError as exc:
                raise AdminConfigUnavailable(f"无法读取配置文件 {path}: {exc}") from exc
        if self.target_path is not None:
            digest.update(b"targets\0")
            try:
                digest.update(self.target_path.read_bytes() if self.target_path.exists() else b"[]\n")
            except OSError as exc:
                raise AdminConfigUnavailable("无法读取自管 Prometheus 目标文件") from exc
        return digest.hexdigest()

    def _load_docs(self) -> dict[str, Any]:
        yaml = self._yaml()
        docs: dict[str, Any] = {}
        for name, path in self.paths.items():
            try:
                with path.open("r", encoding="utf-8") as handle:
                    docs[name] = yaml.load(handle) or {}
            except OSError as exc:
                raise AdminConfigUnavailable(f"无法读取配置文件 {path}: {exc}") from exc
            if not isinstance(docs[name], Mapping):
                raise AdminConfigUnavailable(f"配置文件顶层必须是映射: {path}")
        return docs

    def _dump(self, document: Any) -> str:
        stream = io.StringIO()
        self._yaml().dump(document, stream)
        return stream.getvalue()

    @staticmethod
    def _rule_id(rule: Mapping[str, Any], index: int) -> str:
        existing = str(rule.get("id") or "").strip()
        if existing:
            return existing
        material = json.dumps(_plain(rule), ensure_ascii=False, sort_keys=True, default=str)
        suffix = hashlib.sha256(f"{index}:{material}".encode("utf-8")).hexdigest()[:12]
        return f"rule_{suffix}"

    @staticmethod
    def _commands(document: Mapping[str, Any], *, allow_sudo: bool) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for key, raw in (document.get("commands") or {}).items():
            spec = _mapping(raw, f"commands.{key}")
            params = []
            for name, param in (spec.get("params") or {}).items():
                item = {"name": str(name)}
                item.update(_plain(param))
                params.append(item)
            item = {
                "key": str(key),
                "enabled": spec.get("enabled", True),
                "summary": str(spec.get("summary") or ""),
                "template": str(spec.get("template") or ""),
                "params": params,
            }
            if allow_sudo:
                item["sudo"] = bool(spec.get("sudo", False))
            output.append(item)
        return output

    def snapshot(self) -> dict[str, Any]:
        """返回结构化草稿与 revision；不输出主机连接字段或 Token 明文。"""
        docs = self._load_docs()
        settings_data = _plain(self._yaml().load(interpolate(self._dump(docs["settings"]))) or {})
        effective = _build_settings(settings_data)
        tokens = parse_tokens(effective.auth.tokens)
        identities = sorted({identity for _, identity in tokens})
        hosts = parse_hosts(self._dump(docs["hosts"]))
        by_instance = {host.monitor.instance: host.host_id for host in hosts.all()}

        rules: list[dict[str, Any]] = []
        for index, raw in enumerate(docs["rules"].get("rules") or []):
            rule = _mapping(raw, f"rules[{index}]")
            params = {
                str(key): _plain(value)
                for key, value in (rule.get("params") or {}).items()
                if key not in CONNECTION_FIELDS
            }
            rules.append({
                "id": self._rule_id(rule, index),
                "enabled": rule.get("enabled", True),
                "alertname": str(rule.get("alertname") or ""),
                "action": str(rule.get("action") or ""),
                "host_id": by_instance.get(str(rule.get("target_instance") or ""), ""),
                "params": params,
                "auto_execute": bool(rule.get("auto_execute", False)),
                "description": str(rule.get("description") or ""),
            })

        current_revision = self.revision()
        active_revision = self.runtime.current.revision if self.runtime else self.active_revision
        return {
            "revision": current_revision,
            "active_revision": active_revision,
            "restart_required": current_revision != active_revision,
            "available_identities": identities,
            "config": {
                "settings": {
                    "auth_enabled": effective.auth.enabled,
                    "approval_mode": effective.auth.approval_mode,
                    "approver_identities": list(parse_approvers(effective.auth.approver_identities)),
                    "admin_identities": list(parse_admins(effective.auth.admin_identities)),
                    "alert_identity": effective.auth.alert_identity,
                    "llm_enabled": effective.llm.enabled,
                    "agent_enabled": effective.agent.enabled,
                    "inspection_enabled": effective.inspection.enabled,
                },
                "automation": {
                    "enabled": bool(docs["rules"].get("auto_execute_enabled", True)),
                },
                "catalogs": {
                    public_name: {
                        "enabled": bool(docs[file_name].get("enabled", True)),
                        "commands": self._commands(docs[file_name], allow_sudo=allow_sudo),
                    }
                    for public_name, (file_name, allow_sudo) in _CATALOGS.items()
                },
                "hosts": [
                    {
                        "host_id": host.host_id,
                        "name": host.name,
                        "enabled": host.enabled,
                        "allow": {
                            "diagnostics": sorted(host.capabilities.diagnostics),
                            "rule_actions": sorted(host.capabilities.rule_actions),
                            "agent_actions": sorted(host.capabilities.agent_actions),
                        },
                    }
                    for host in hosts.all()
                ],
                "rules": rules,
            },
        }

    @staticmethod
    def _identity_list(value: Any, what: str, available: set[str], *, required: bool) -> list[str]:
        result: list[str] = []
        for raw in _list(value, what):
            identity = _text(raw, f"{what}[]", required=True, maximum=128)
            if identity not in available:
                raise AdminConfigError(f"{what} 包含没有 Token 映射的身份: {identity}")
            if identity not in result:
                result.append(identity)
        if required and not result:
            raise AdminConfigError(f"{what} 至少需要一个身份")
        return result

    def _apply_settings(self, document: Any, config: Mapping[str, Any]) -> None:
        """只写公开开关与身份名；值未变化时保留原 YAML 环境变量表达式。"""
        section = _mapping(config.get("settings"), "config.settings")
        auth = document["auth"]
        tokens = parse_tokens(interpolate(str(auth.get("tokens") or "")))
        available = {identity for _, identity in tokens}
        admins = self._identity_list(
            section.get("admin_identities"), "admin_identities", available, required=True
        )
        approvers = self._identity_list(
            section.get("approver_identities"), "approver_identities", available, required=False
        )
        mode = _text(section.get("approval_mode"), "approval_mode", required=True, maximum=32)
        if mode not in {"owner", "approvers_only"}:
            raise AdminConfigError("approval_mode 只支持 owner / approvers_only")
        if mode == "approvers_only" and not approvers:
            raise AdminConfigError("approvers_only 模式至少需要一个审批人")
        auth_enabled = _bool(section.get("auth_enabled"), "auth_enabled")
        if bool(auth.get("enabled")) != auth_enabled:
            auth["enabled"] = auth_enabled
        if str(auth.get("approval_mode") or "") != mode:
            auth["approval_mode"] = mode
        if tuple(approvers) != parse_approvers(interpolate(str(auth.get("approver_identities") or ""))):
            auth["approver_identities"] = json.dumps(approvers, ensure_ascii=False)
        if tuple(admins) != parse_admins(interpolate(str(auth.get("admin_identities") or ""))):
            auth["admin_identities"] = json.dumps(admins, ensure_ascii=False)
        alert_identity = _text(section.get("alert_identity", ""), "alert_identity", maximum=128)
        if interpolate(str(auth.get("alert_identity") or "")) != alert_identity:
            auth["alert_identity"] = alert_identity
        for name, key in (("llm", "llm_enabled"), ("agent", "agent_enabled"), ("inspection", "inspection_enabled")):
            value = _bool(section.get(key), key)
            if bool(document[name].get("enabled")) != value:
                document[name]["enabled"] = value

    @staticmethod
    def _param_spec(raw: Any, what: str) -> tuple[str, dict[str, Any]]:
        param = _mapping(raw, what)
        name = _text(param.get("name"), f"{what}.name", required=True, maximum=64)
        spec: dict[str, Any] = {
            "type": _text(param.get("type", "str"), f"{what}.type", required=True, maximum=8),
            "description": _text(param.get("description", ""), f"{what}.description", maximum=500),
        }
        for key in ("pattern", "default", "min", "max", "required"):
            if key in param and param[key] not in (None, ""):
                spec[key] = param[key]
        return name, spec

    def _apply_catalog(self, document: Any, raw: Any, what: str, *, allow_sudo: bool) -> None:
        catalog = _mapping(raw, what)
        document["enabled"] = _bool(catalog.get("enabled"), f"{what}.enabled")
        commands = _list(catalog.get("commands"), f"{what}.commands")
        existing = document.get("commands") or {}
        seen: set[str] = set()
        normalized: list[tuple[str, dict[str, Any]]] = []
        for index, raw_command in enumerate(commands):
            command = _mapping(raw_command, f"{what}.commands[{index}]")
            key = _text(command.get("key"), f"{what}.commands[{index}].key", required=True, maximum=64)
            if key in seen:
                raise AdminConfigError(f"{what} 命令名重复: {key}")
            seen.add(key)
            params: dict[str, Any] = {}
            for param_index, raw_param in enumerate(_list(command.get("params", []), f"{what}.{key}.params")):
                name, spec = self._param_spec(raw_param, f"{what}.{key}.params[{param_index}]")
                if name in params:
                    raise AdminConfigError(f"{what}.{key} 参数名重复: {name}")
                params[name] = spec
            spec = {
                "enabled": _bool(command.get("enabled"), f"{what}.{key}.enabled"),
                "summary": _text(command.get("summary", ""), f"{what}.{key}.summary", maximum=500),
                "template": _text(command.get("template"), f"{what}.{key}.template", required=True),
                "params": params,
            }
            if allow_sudo:
                spec["sudo"] = _bool(command.get("sudo", False), f"{what}.{key}.sudo")
            normalized.append((key, spec))
        for key in list(existing):
            if key not in seen:
                del existing[key]
        for key, spec in normalized:
            if key in existing and isinstance(existing[key], Mapping):
                _sync_mapping(existing[key], spec)
            else:
                existing[key] = spec
        document["commands"] = existing

    @staticmethod
    def _apply_hosts(document: Any, raw_hosts: Any) -> None:
        """只合并 allow 白名单；主机、监控目标和 SSH 连接信息保持原样。"""
        incoming = _list(raw_hosts, "config.hosts")
        by_id = {str(item.get("id") or ""): item for item in document.get("hosts") or []}
        if {str(item.get("host_id") or "") for item in incoming} != set(by_id):
            raise AdminConfigError("主机管理只允许修改授权，不能新增、删除或改名主机")
        for raw in incoming:
            item = _mapping(raw, "config.hosts[]")
            host_id = _text(item.get("host_id"), "host_id", required=True, maximum=63)
            allow = _mapping(item.get("allow"), f"hosts.{host_id}.allow")
            target = by_id[host_id]
            target_allow = target.get("allow") or {}
            for name in ("diagnostics", "rule_actions", "agent_actions"):
                values = [_text(value, f"hosts.{host_id}.{name}[]", required=True, maximum=64)
                          for value in _list(allow.get(name, []), f"hosts.{host_id}.{name}")]
                if len(values) != len(set(values)):
                    raise AdminConfigError(f"hosts.{host_id}.{name} 不能重复")
                if list(target_allow.get(name) or []) != values:
                    target_allow[name] = values
            target["allow"] = target_allow

    def _apply_rules(self, document: Any, raw_rules: Any, hosts_document: Any) -> None:
        rules = _list(raw_rules, "config.rules")
        registry = parse_hosts(self._dump(hosts_document))
        existing_list = document.get("rules") or []
        existing = {
            self._rule_id(_mapping(rule, "rules[]"), index): rule
            for index, rule in enumerate(existing_list)
        }
        normalized = []
        seen: set[str] = set()
        for index, raw in enumerate(rules):
            rule = _mapping(raw, f"config.rules[{index}]")
            rule_id = _text(rule.get("id"), f"rules[{index}].id", required=True, maximum=64)
            if rule_id[0] not in "abcdefghijklmnopqrstuvwxyz" or any(ch not in _RULE_ID_CHARS for ch in rule_id):
                raise AdminConfigError(f"规则 id 不合法: {rule_id}")
            if rule_id in seen:
                raise AdminConfigError(f"规则 id 重复: {rule_id}")
            seen.add(rule_id)
            old = existing.get(rule_id)
            old_params = dict((old or {}).get("params") or {})
            hidden = {key: value for key, value in old_params.items() if key in CONNECTION_FIELDS}
            params = _mapping(rule.get("params", {}), f"rules.{rule_id}.params")
            forbidden = set(params) & set(CONNECTION_FIELDS)
            if forbidden:
                raise AdminConfigError("规则参数不允许包含连接字段: " + ", ".join(sorted(forbidden)))
            host_id = _text(rule.get("host_id", ""), f"rules.{rule_id}.host_id", maximum=63)
            if host_id:
                host = registry.get(host_id)
                if host is None:
                    raise AdminConfigError(f"规则 {rule_id} 引用了未知主机 {host_id}")
                target_instance = host.monitor.instance
            else:
                target_instance = str((old or {}).get("target_instance") or "")
            item = old if isinstance(old, Mapping) else {}
            values = {
                "id": rule_id,
                "enabled": _bool(rule.get("enabled"), f"rules.{rule_id}.enabled"),
                "alertname": _text(rule.get("alertname"), f"rules.{rule_id}.alertname", required=True, maximum=256),
                "action": _text(rule.get("action"), f"rules.{rule_id}.action", required=True, maximum=64),
                "target_instance": target_instance,
                "params": {**hidden, **_plain(params)},
                "auto_execute": _bool(rule.get("auto_execute"), f"rules.{rule_id}.auto_execute"),
                "description": _text(rule.get("description", ""), f"rules.{rule_id}.description", maximum=1000),
            }
            _sync_mapping(item, values)
            normalized.append(item)
        document["rules"] = normalized

    def _validate(self, docs: Mapping[str, Any]) -> None:
        """先验证完整候选集，再触碰磁盘；包含各目录与规则/主机的跨文件引用。"""
        import yaml

        settings_raw = yaml.safe_load(interpolate(self._dump(docs["settings"]))) or {}
        settings = _build_settings(settings_raw)
        tokens = parse_tokens(settings.auth.tokens)
        identities = {identity for _, identity in tokens}
        admins = set(parse_admins(settings.auth.admin_identities))
        if not admins or not admins <= identities:
            raise AdminConfigError("管理员身份必须非空且全部存在于 Token 身份映射")
        parse_approvers(settings.auth.approver_identities)
        registry = parse_hosts(self._dump(docs["hosts"]))
        parse_actions(self._dump(docs["actions"]))
        parse_agent_actions(self._dump(docs["agent_actions"]))
        parse_diagnostics(self._dump(docs["diagnostics"]))
        if "metrics" in docs:
            parse_metrics_config(self._dump(docs["metrics"]))
        rules_data = yaml.safe_load(interpolate(self._dump(docs["rules"]))) or {}
        rules = RulesFile(**rules_data)

        command_sets: dict[str, set[str]] = {}
        action_defs: dict[str, Any] = {}
        for public_name, (file_name, allow_sudo) in _CATALOGS.items():
            raw_commands = docs[file_name].get("commands") or {}
            command_sets[public_name] = set(raw_commands)
            parsed = {
                str(key): parse_command_def(str(key), spec, allow_sudo=allow_sudo)
                for key, spec in raw_commands.items()
            }
            if public_name == "rule_actions":
                action_defs = parsed
        for host in registry.all():
            for capability in ("diagnostics", "rule_actions", "agent_actions"):
                unknown = set(getattr(host.capabilities, capability)) - command_sets[capability]
                if unknown:
                    raise AdminConfigError(
                        f"主机 {host.host_id} 的 {capability} 引用了未知命令: {', '.join(sorted(unknown))}"
                    )
        for rule in rules.rules:
            command = action_defs.get(rule.action)
            if command is None:
                raise AdminConfigError(f"规则 {rule.id or rule.alertname} 引用了未知规则动作 {rule.action}")
            safe_params = {key: value for key, value in rule.params.items() if key not in CONNECTION_FIELDS}
            try:
                validated_params(command, safe_params)
            except Exception as exc:
                raise AdminConfigError(f"规则 {rule.id or rule.alertname} 参数无效: {exc}") from exc
            if rule.auto_execute and not rule.target_instance:
                raise AdminConfigError(f"自动执行规则 {rule.id or rule.alertname} 必须选择目标主机")

    def _candidate(self, docs: Mapping[str, Any]) -> RuntimeSnapshot | None:
        """在写盘前装配候选运行态；不支持热切换的 Settings 差异整批拒绝。

        新增 Settings 字段时必须审查下面的 hot_fields：未列入的字段不会被部分应用。
        冻结的 YAML 文本交给新 Container，避免惰性组件随后读到更新后的磁盘文件。
        """
        if self.runtime is None:
            return None
        import yaml
        from src.container import Container
        from src.main import _check_auth

        old = self.runtime.current
        settings = _build_settings(
            yaml.safe_load(interpolate(self._dump(docs["settings"]))) or {}
        )
        # 测试及嵌入式运行可通过 create_app 显式注入配置路径。
        if not settings.hosts.file and old.settings.hosts.file:
            settings = replace(settings, hosts=old.settings.hosts)
        if not settings.metrics.file and old.settings.metrics.file:
            settings = replace(settings, metrics=old.settings.metrics)
        before = asdict(old.settings)
        after = asdict(settings)
        hot_fields = {
            "auth.enabled", "auth.approval_mode", "auth.approver_identities",
            "auth.admin_identities", "auth.alert_identity",
            "prometheus.base_url", "prometheus.timeout", "monitor.provider",
            "llm.enabled", "llm.provider", "agent.enabled", "agent.name",
            "agent.debug", "agent.recursion_limit", "chat.max_message_chars",
            "alert.trend_minutes", "alert.trend_step",
            "inspection.enabled", "inspection.interval_seconds",
            "inspection.run_on_start", "inspection.max_hosts_per_run",
            "inspection.max_queries_per_run", "inspection.round_timeout_seconds",
        }
        restart_fields = [
            f"{section}.{field}"
            for section in before
            for field in before[section]
            if before[section][field] != after[section][field]
            and f"{section}.{field}" not in hot_fields
        ]
        if restart_fields:
            raise AdminConfigError(
                "以下配置暂需重启，未应用任何字段: " + ", ".join(restart_fields)
            )
        _check_auth(settings)
        texts = {name: self._dump(doc) for name, doc in docs.items()}
        container = Container(settings, config_texts=texts, shared_from=old.container)
        container.metrics_config()
        container.hosts()
        container.executors()
        container.diagnostics()
        container.agent_actions()
        agent = container.aiops_assistant()
        return RuntimeSnapshot("", settings, container, agent)

    def apply_disk(self, *, expected_revision: str, actor: str) -> dict[str, Any]:
        """管理员显式应用手动修改的 YAML；不支持热切换的字段整批拒绝。"""
        with self._lock:
            current = self.revision()
            if expected_revision != current:
                raise AdminConfigConflict("磁盘配置已变化，请重新加载")
            docs = self._load_docs()
            self._validate(docs)
            candidate = self._candidate(docs)
            if self.revision() != current:
                raise AdminConfigConflict("校验期间磁盘配置已变化，请重新加载")
            if self.runtime is not None:
                self.runtime.begin_apply()
                try:
                    self.runtime.commit(replace(candidate, revision=current))
                except Exception:
                    self.runtime.cancel_apply()
                    raise
            self.active_revision = current
            logger.info("管理员 %s 应用磁盘配置 revision=%s", actor, current[:12])
            return self.snapshot()

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        """在目标目录创建临时文件，刷盘后替换单个文件；不保证跨文件原子性。"""
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def update(self, *, expected_revision: str, config: Mapping[str, Any], actor: str) -> dict[str, Any]:
        """在进程内锁中校验 revision、合并和保存整份草稿。

        revision 防止正常并发请求静默覆盖；若其它进程绕开本锁直接写配置，保存期间
        仍可能发生竞态，因此当前部署约束是单 worker、单实例。写入异常时回滚已替换
        文件；备份保留供人工检查，但本接口不提供恢复或进程重启按钮。
        """
        with self._lock:
            current = self.revision()
            if expected_revision != current:
                raise AdminConfigConflict("配置已被其他操作修改，请重新加载后再保存")
            docs = self._load_docs()
            root = _mapping(config, "config")
            self._apply_settings(docs["settings"], root)
            automation = _mapping(root.get("automation"), "config.automation")
            docs["rules"]["auto_execute_enabled"] = _bool(
                automation.get("enabled"), "automation.enabled"
            )
            catalogs = _mapping(root.get("catalogs"), "config.catalogs")
            for public_name, (file_name, allow_sudo) in _CATALOGS.items():
                self._apply_catalog(
                    docs[file_name], catalogs.get(public_name), f"catalogs.{public_name}",
                    allow_sudo=allow_sudo,
                )
            self._apply_hosts(docs["hosts"], root.get("hosts"))
            self._apply_rules(docs["rules"], root.get("rules"), docs["hosts"])
            self._validate(docs)
            candidate = self._candidate(docs)
            if self.revision() != current:
                raise AdminConfigConflict("校验期间磁盘配置已变化，请重新加载")

            rendered = {name: self._dump(document) for name, document in docs.items()}
            changed = [name for name, path in self.paths.items()
                       if path.read_text(encoding="utf-8") != rendered[name]]
            if self.revision() != current:
                raise AdminConfigConflict("保存前磁盘配置已变化，请重新加载")
            if not changed:
                if self.runtime is not None and current != self.runtime.current.revision:
                    self.runtime.begin_apply()
                    try:
                        self.runtime.commit(replace(candidate, revision=current))
                    except Exception:
                        self.runtime.cancel_apply()
                        raise
                result = self.snapshot()
                result.update({"changed_files": [], "backup_id": None})
                return result

            if self.runtime is not None:
                self.runtime.begin_apply()
            backup_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup_dir = self.backup_root / backup_id
            attempted: list[str] = []
            try:
                backup_dir.mkdir(parents=True, exist_ok=False)
                for name in changed:
                    shutil.copy2(self.paths[name], backup_dir / self.paths[name].name)
                for name in changed:
                    attempted.append(name)
                    self._atomic_write(self.paths[name], rendered[name])
                new_revision = self.revision()
            except Exception:
                try:
                    for name in reversed(attempted):
                        backup = backup_dir / self.paths[name].name
                        self._atomic_write(self.paths[name], backup.read_bytes().decode("utf-8"))
                finally:
                    if self.runtime is not None:
                        self.runtime.cancel_apply()
                raise
            if self.runtime is not None:
                self.runtime.commit(replace(candidate, revision=new_revision))
            self.active_revision = new_revision
            logger.info("管理员 %s 更新配置 sections=%s backup=%s", actor, changed, backup_id)
            result = self.snapshot()
            result.update({
                "changed_files": [_display_path(self.paths[name]) for name in changed],
                "backup_id": backup_id,
            })
            return result
