"""严格加载静态 hosts.yaml，并提供旧单主机兼容注册表。"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from src.interfaces.host import (
    Host,
    HostCapabilities,
    HostConfigError,
    HostRegistry,
    HostSshTarget,
    MonitorTarget,
)
from src.plugins.common.endpoint import DEFAULT_CONNECT_TIMEOUT
from src.utils.config_loader import ROOT_DIR, parse_yaml

HOST_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
LEGACY_HOST_ID = "legacy-default"
_TOP_KEYS = {"version", "credentials", "hosts"}
_CREDENTIAL_KEYS = {"username", "password", "key_filename", "known_hosts"}
_HOST_KEYS = {"id", "name", "enabled", "prometheus", "ssh", "allow"}
_PROM_KEYS = {"job", "instance"}
_SSH_KEYS = {"host", "port", "credential_ref", "connect_timeout"}
_ALLOW_KEYS = {"diagnostics", "rule_actions", "agent_actions"}


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HostConfigError(f"{what} 必须是映射")
    return value


def _unknown(data: Mapping[str, Any], allowed: set[str], what: str) -> None:
    extra = set(data) - allowed
    if extra:
        raise HostConfigError(f"{what} 存在未知字段: {', '.join(sorted(extra))}")


def _text(value: Any, what: str, *, required: bool = True) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise HostConfigError(f"{what} 必须是字符串")
    result = unicodedata.normalize("NFKC", value).strip()
    if required and not result:
        raise HostConfigError(f"{what} 不能为空")
    if any(unicodedata.category(ch) in {"Cc", "Cf"} for ch in result):
        raise HostConfigError(f"{what} 含控制或不可见字符")
    return result


def _password(value: Any, what: str) -> str | None:
    # 密码是认证凭据，首尾空白和 Unicode 字符都必须原样传给 SSH。
    if value is None:
        return None
    if not isinstance(value, str):
        raise HostConfigError(f"{what} 必须是字符串")
    return value or None


def _positive_int(value: Any, what: str, default: int) -> int:
    if isinstance(value, bool):
        raise HostConfigError(f"{what} 必须是正整数")
    try:
        result = int(default if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise HostConfigError(f"{what} 必须是正整数") from exc
    if result < 1:
        raise HostConfigError(f"{what} 必须大于等于 1")
    return result


def _string_set(value: Any, what: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise HostConfigError(f"{what} 必须是字符串数组")
    result = [_text(item, f"{what}[]") for item in value]
    if len(result) != len(set(result)):
        raise HostConfigError(f"{what} 不能包含重复项")
    return frozenset(result)


def parse_hosts(text: str, *, credential_overrides: Mapping[str, Mapping[str, Any]] | None = None) -> HostRegistry:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise HostConfigError("缺少 pyyaml，无法解析 hosts.yaml") from exc
    try:
        raw = parse_yaml(text) or {}
    except Exception as exc:
        raise HostConfigError(f"hosts.yaml 解析失败: {exc}") from exc
    data = _mapping(raw, "hosts.yaml 顶层")
    _unknown(data, _TOP_KEYS, "hosts.yaml")
    if data.get("version") != 1:
        raise HostConfigError("hosts.yaml version 当前只支持 1")

    # 页面凭据在解析后覆盖；其明文不得再次做环境变量插值或写回 YAML。
    raw_credentials = {**_mapping(data.get("credentials") or {}, "credentials"),
                       **(credential_overrides or {})}
    credentials: dict[str, Mapping[str, Any]] = {}
    for key, value in raw_credentials.items():
        cred_id = _text(key, "credential id")
        spec = _mapping(value, f"credentials.{cred_id}")
        _unknown(spec, _CREDENTIAL_KEYS, f"credentials.{cred_id}")
        credentials[cred_id] = spec

    raw_hosts = data.get("hosts")
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise HostConfigError("hosts 必须是非空数组")
    hosts: list[Host] = []
    ids: set[str] = set()
    enabled_instances: set[str] = set()
    enabled_pairs: set[tuple[str, str]] = set()
    for index, value in enumerate(raw_hosts):
        spec = _mapping(value, f"hosts[{index}]")
        _unknown(spec, _HOST_KEYS, f"hosts[{index}]")
        host_id = _text(spec.get("id"), f"hosts[{index}].id")
        if not HOST_ID_RE.fullmatch(host_id) or host_id == LEGACY_HOST_ID:
            raise HostConfigError(f"非法或保留的 host id: {host_id!r}")
        if host_id in ids:
            raise HostConfigError(f"重复 host id: {host_id}")
        ids.add(host_id)
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise HostConfigError(f"hosts[{index}].enabled 必须是布尔值")
        prom = _mapping(spec.get("prometheus"), f"hosts[{index}].prometheus")
        _unknown(prom, _PROM_KEYS, f"hosts[{index}].prometheus")
        job = _text(prom.get("job"), f"hosts[{index}].prometheus.job")
        instance = _text(prom.get("instance"), f"hosts[{index}].prometheus.instance")
        if enabled:
            if instance in enabled_instances:
                raise HostConfigError(f"启用主机的 Prometheus instance 重复: {instance}")
            if (job, instance) in enabled_pairs:
                raise HostConfigError(f"启用主机的 Prometheus job/instance 重复: {job}/{instance}")
            enabled_instances.add(instance)
            enabled_pairs.add((job, instance))

        endpoint = None
        credential_ref = ""
        ssh_raw = spec.get("ssh")
        if ssh_raw is not None:
            ssh = _mapping(ssh_raw, f"hosts[{index}].ssh")
            _unknown(ssh, _SSH_KEYS, f"hosts[{index}].ssh")
            credential_ref = _text(ssh.get("credential_ref"), f"hosts[{index}].ssh.credential_ref")
            credential = credentials.get(credential_ref)
            if credential is None:
                raise HostConfigError(f"主机 {host_id} 引用未知 SSH credential: {credential_ref}")
            endpoint = HostSshTarget(
                host=_text(ssh.get("host"), f"hosts[{index}].ssh.host"),
                port=_positive_int(ssh.get("port"), f"hosts[{index}].ssh.port", 22),
                username=_text(credential.get("username", "root"), f"credentials.{credential_ref}.username"),
                password=_password(credential.get("password"), f"credentials.{credential_ref}.password"),
                key_filename=_text(credential.get("key_filename", ""), f"credentials.{credential_ref}.key_filename", required=False) or None,
                known_hosts=_text(credential.get("known_hosts", ""), f"credentials.{credential_ref}.known_hosts", required=False) or None,
                connect_timeout=_positive_int(ssh.get("connect_timeout"), f"hosts[{index}].ssh.connect_timeout", DEFAULT_CONNECT_TIMEOUT),
            )
        allow = _mapping(spec.get("allow") or {}, f"hosts[{index}].allow")
        _unknown(allow, _ALLOW_KEYS, f"hosts[{index}].allow")
        capabilities = HostCapabilities(
            diagnostics=_string_set(allow.get("diagnostics"), f"hosts[{index}].allow.diagnostics"),
            rule_actions=_string_set(allow.get("rule_actions"), f"hosts[{index}].allow.rule_actions"),
            agent_actions=_string_set(allow.get("agent_actions"), f"hosts[{index}].allow.agent_actions"),
        )
        if endpoint is None and any((capabilities.diagnostics, capabilities.rule_actions, capabilities.agent_actions)):
            raise HostConfigError(f"主机 {host_id} 未配置 SSH，却声明了 SSH 能力授权")
        hosts.append(Host(
            host_id=host_id,
            name=_text(spec.get("name"), f"hosts[{index}].name"),
            enabled=enabled,
            monitor=MonitorTarget(job=job, instance=instance),
            ssh=endpoint,
            credential_ref=credential_ref,
            capabilities=capabilities,
        ))
    if not any(host.enabled for host in hosts):
        raise HostConfigError("hosts.yaml 至少需要一台启用主机")
    return HostRegistry(tuple(hosts))


def legacy_registry(*, instance: str = "localhost:9100") -> HostRegistry:
    """从既有环境变量合成单主机；只用于未配置 hosts.yaml 的兼容路径。"""
    endpoint = HostSshTarget(
        host=os.getenv("SSH_HOST", "localhost").strip() or "localhost",
        port=_positive_int(os.getenv("SSH_PORT"), "SSH_PORT", 22),
        username=os.getenv("SSH_USER", "ops").strip() or "ops",
        password=os.getenv("SSH_PASSWORD") or None,
        key_filename=os.getenv("SSH_KEY_FILENAME") or None,
        known_hosts=os.getenv("SSH_KNOWN_HOSTS") or None,
        connect_timeout=DEFAULT_CONNECT_TIMEOUT,
    )
    return HostRegistry((Host(
        host_id=LEGACY_HOST_ID,
        name="默认主机",
        enabled=True,
        monitor=MonitorTarget(job="node", instance=instance or "localhost:9100"),
        ssh=endpoint,
        credential_ref="legacy-environment",
        legacy=True,
    ),))


def load_host_registry(path: str | Path | None, *, legacy_instance: str) -> HostRegistry:
    raw = str(path or "").strip() or os.getenv("OPS_HOSTS_FILE", "").strip()
    if not raw:
        return legacy_registry(instance=legacy_instance)
    file_path = Path(raw)
    if not file_path.is_absolute():
        file_path = ROOT_DIR / file_path
    if not file_path.is_file():
        raise HostConfigError(f"主机清单不存在: {file_path}")
    return parse_hosts(file_path.read_text(encoding="utf-8"))
