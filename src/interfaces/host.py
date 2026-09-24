"""多主机清单使用的中性类型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

class HostError(Exception):
    """主机不存在、禁用或无法唯一解析。"""


class HostConfigError(ValueError):
    """主机清单结构或安全约束无效。"""


@dataclass(frozen=True)
class MonitorTarget:
    job: str
    instance: str


@dataclass(frozen=True)
class HostSshTarget:
    """主机清单中的 SSH 目标；不暴露给 API 或模型。"""

    host: str
    port: int = 22
    username: str = "root"
    key_filename: str | None = None
    known_hosts: str | None = None
    password: str | None = None
    connect_timeout: int = 8


@dataclass(frozen=True)
class HostCapabilities:
    diagnostics: frozenset[str] = field(default_factory=frozenset)
    rule_actions: frozenset[str] = field(default_factory=frozenset)
    agent_actions: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Host:
    host_id: str
    name: str
    enabled: bool
    monitor: MonitorTarget
    ssh: HostSshTarget | None = None
    credential_ref: str = ""
    capabilities: HostCapabilities = HostCapabilities()
    legacy: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """只返回页面可用的主机摘要，不泄露 SSH 目标和凭据。

        Prometheus 的 job/instance 是监控匹配标签，可能包含内网地址；API 调用方仍应
        把整个响应当内部运维数据保管，不能因“公开摘要”一词就公开分享。
        """
        return {
            "host_id": self.host_id,
            "name": self.name,
            "enabled": self.enabled,
            "prometheus": {"job": self.monitor.job, "instance": self.monitor.instance},
            "ssh_configured": self.ssh is not None,
            "capabilities": {
                "diagnostics": sorted(self.capabilities.diagnostics),
                "rule_actions": sorted(self.capabilities.rule_actions),
                "agent_actions": sorted(self.capabilities.agent_actions),
            },
            "legacy": self.legacy,
        }


@dataclass(frozen=True)
class HostResolution:
    host: Host | None
    status: str

    @property
    def host_id(self) -> str | None:
        return self.host.host_id if self.host is not None else None


class HostRegistry:
    """不可变主机注册表；所有外部目标都必须先经这里解析。"""

    def __init__(self, hosts: tuple[Host, ...]) -> None:
        self._hosts = hosts
        self._by_id = {host.host_id: host for host in hosts}

    def all(self, *, include_disabled: bool = True) -> tuple[Host, ...]:
        return self._hosts if include_disabled else tuple(h for h in self._hosts if h.enabled)

    def enabled(self) -> tuple[Host, ...]:
        return self.all(include_disabled=False)

    def get(self, host_id: str) -> Host | None:
        return self._by_id.get(host_id)

    def require(self, host_id: str, *, enabled: bool = True) -> Host:
        host = self.get(host_id)
        if host is None:
            raise HostError(f"未知主机 ID: {host_id}")
        if enabled and not host.enabled:
            raise HostError(f"主机已禁用: {host_id}")
        return host

    def resolve_name(self, name: str) -> Host:
        matches = [host for host in self.enabled() if host.name == name]
        if not matches:
            raise HostError(f"未知主机名称: {name}")
        if len(matches) != 1:
            raise HostError(f"主机名称存在歧义，请改用 host_id: {name}")
        return matches[0]

    def resolve_labels(self, labels: Mapping[str, str]) -> HostResolution:
        instance = str(labels.get("instance") or "").strip()
        job = str(labels.get("job") or "").strip()
        if not instance:
            return HostResolution(None, "missing_instance")
        matches = [host for host in self.enabled() if host.monitor.instance == instance]
        if job:
            matches = [host for host in matches if host.monitor.job == job]
        if len(matches) == 1:
            return HostResolution(matches[0], "matched")
        return HostResolution(None, "ambiguous" if matches else "unknown")
