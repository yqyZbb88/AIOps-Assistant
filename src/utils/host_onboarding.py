"""管理员新增监控主机：目标发现、脱敏预览、SSH 认证测试与写盘。

只负责新增，不提供删除/停用。managed 模式可能同时写 hosts.yaml 和
Prometheus file_sd 目标文件；单文件原子替换加异常回滚并非断电原子事务。
登记主机不安装或启动远程 Exporter，Prometheus 后续发现目标也不代表 up=1。
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from src.plugins.common.ssh import _new_client
from src.utils.admin_config import AdminConfigConflict, AdminConfigError, AdminConfigUnavailable
from src.utils.hosts_config import HOST_ID_RE, LEGACY_HOST_ID, parse_hosts
from src.utils.runtime import RuntimeSnapshot

_ADDRESS = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9a-fA-F:]+\]):[0-9]{1,5}$")
_MANAGED_JOB = "aiops-hosts"


def _address_host(address: str) -> str:
    if not _ADDRESS.fullmatch(address):
        raise AdminConfigError("Exporter 目标必须是有效的 主机:端口")
    parsed = urlsplit("//" + address)
    try:
        host, port = parsed.hostname, parsed.port
    except ValueError as exc:
        raise AdminConfigError("Exporter 目标端口无效") from exc
    if not host or not port or port > 65535:
        raise AdminConfigError("Exporter 目标端口无效")
    return host


class HostOnboarding:
    """复用管理器的进程内写锁、revision、备份与运行快照。

    SSH 检查票据仅保存在此对象内 5 分钟，重启或换进程后失效；它证明一次
    主机密钥/认证握手通过，不证明远程诊断命令或 Exporter 工作正常。
    """

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self._verified: dict[str, tuple[str, float]] = {}

    def _mode(self) -> str:
        return self.manager.runtime.current.settings.prometheus.target_mode

    def _targets(self) -> list[dict[str, Any]]:
        """读取 Prometheus 已发现目标；仅返回登记所需标签，不返回原始响应。"""
        import requests

        settings = self.manager.runtime.current.settings.prometheus
        try:
            response = requests.get(
                settings.base_url.rstrip("/") + "/api/v1/targets",
                params={"state": "active"}, timeout=settings.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping) or payload.get("status") != "success":
                raise ValueError("target response")
            raw = payload["data"]["activeTargets"]
            if not isinstance(raw, list):
                raise ValueError("target list")
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            # Prometheus URL 与可能携带的认证信息不应出现在管理 API。
            raise AdminConfigUnavailable("Prometheus 目标查询失败") from exc
        targets = []
        for target in raw:
            if not isinstance(target, Mapping):
                continue
            labels = target.get("labels") or {}
            discovered = target.get("discoveredLabels") or {}
            if not isinstance(labels, Mapping) or not isinstance(discovered, Mapping):
                continue
            job, instance = str(labels.get("job") or ""), str(labels.get("instance") or "")
            address = str(discovered.get("__address__") or "")
            if not job or not instance:
                continue
            try:
                actual_host = _address_host(address)
                instance_host = _address_host(instance)
                ssh_eligible = actual_host == instance_host
            except AdminConfigError:
                ssh_eligible = False
            targets.append({
                "job": job, "instance": instance, "address": address,
                "health": "up" if target.get("health") == "up" else "down",
                "ssh_eligible": ssh_eligible,
            })
        return targets

    def options(self) -> dict[str, Any]:
        manager = self.manager
        docs = manager._load_docs()
        mode = self._mode()
        try:
            targets = self._targets()
            warning = ""
        except AdminConfigUnavailable:
            if mode == "external":
                raise
            targets, warning = [], "Prometheus 暂不可用；可登记新目标，发现状态稍后确认"
        diagnostics = docs["diagnostics"].get("commands") or {}
        return {
            "revision": manager.revision(),
            "mode": mode,
            "hosts": [
                {"host_id": host.host_id, "name": host.name,
                 "job": host.monitor.job, "instance": host.monitor.instance}
                for host in manager.host_registry(docs).all()
            ],
            "targets": targets,
            "credentials": sorted(str(key) for key in manager.credential_specs(docs)),
            "diagnostics": sorted(
                str(key) for key, value in diagnostics.items()
                if docs["diagnostics"].get("enabled", True) and value.get("enabled", True)
            ),
            "warning": warning,
        }

    def _prepare(self, draft: Mapping[str, Any], docs: Mapping[str, Any]) -> tuple[dict[str, Any], bool, str]:
        manager = self.manager
        host_id = str(draft.get("host_id") or "").strip()
        name = str(draft.get("name") or "").strip()
        if not HOST_ID_RE.fullmatch(host_id) or host_id == LEGACY_HOST_ID:
            raise AdminConfigError("host_id 必须是小写字母开头、最长 63 位的字母数字或连字符")
        if not name or len(name) > 128:
            raise AdminConfigError("显示名不能为空且不得超过 128 字符")
        if any(ord(char) < 32 for char in name):
            raise AdminConfigError("显示名不能包含控制字符")
        mode = self._mode()
        address = str(draft.get("exporter_address") or "").strip()
        if address:
            if mode != "managed":
                raise AdminConfigError("外部 Prometheus 只能选择已发现目标")
            _address_host(address)
            job, instance, health, eligible = _MANAGED_JOB, address, "down", True
            new_target = True
        else:
            job = str(draft.get("job") or "").strip()
            instance = str(draft.get("instance") or "").strip()
            matches = [item for item in self._targets()
                       if item["job"] == job and item["instance"] == instance]
            if len(matches) != 1:
                raise AdminConfigError("所选 Prometheus 目标不存在或不唯一，请刷新目标列表")
            target = matches[0]
            address, health, eligible = target["address"], target["health"], target["ssh_eligible"]
            new_target = False
        registry = manager.host_registry(docs)
        if any(h.monitor.instance == instance for h in registry.all()):
            old = registry.get(host_id)
            if old is None or old.monitor.job != job or old.monitor.instance != instance:
                raise AdminConfigConflict("该 Prometheus 目标已登记给其他主机")
        cred = str(draft.get("credential_id") or "").strip()
        diagnostics = draft.get("diagnostics") or []
        if not isinstance(diagnostics, list) or any(not isinstance(key, str) for key in diagnostics):
            raise AdminConfigError("诊断授权必须是命令键数组")
        if len(set(diagnostics)) != len(diagnostics):
            raise AdminConfigError("诊断授权不能重复")
        available = {
            str(key) for key, value in (docs["diagnostics"].get("commands") or {}).items()
            if docs["diagnostics"].get("enabled", True) and value.get("enabled", True)
        }
        if not set(diagnostics) <= available:
            raise AdminConfigError("诊断授权包含未知或已禁用命令")
        ssh = None
        if cred:
            if not eligible:
                raise AdminConfigError("该代理抓取目标不能安全推导 SSH 主机")
            if cred not in manager.credential_specs(docs):
                raise AdminConfigError("SSH 凭据 ID 不存在")
            credential = manager.credential_specs(docs)[cred]
            if not credential.get("password") and not credential.get("key_filename"):
                raise AdminConfigError("所选 SSH 凭据没有显式密码或密钥")
            try:
                port = int(draft.get("ssh_port", 22))
            except (TypeError, ValueError) as exc:
                raise AdminConfigError("SSH 端口无效") from exc
            if not 1 <= port <= 65535:
                raise AdminConfigError("SSH 端口必须在 1~65535")
            ssh = {"host": _address_host(address), "port": port, "credential_ref": cred}
        elif diagnostics:
            raise AdminConfigError("勾选诊断前必须选择 SSH 凭据并通过连接检查")
        item = {
            "id": host_id, "name": name, "enabled": True,
            "prometheus": {"job": job, "instance": instance},
            "allow": {"diagnostics": diagnostics, "rule_actions": [], "agent_actions": []},
        }
        if ssh:
            item["ssh"] = ssh
        old = registry.get(host_id)
        if old:
            same = (
                old.name == name and old.enabled and old.monitor.job == job
                and old.monitor.instance == instance and old.credential_ref == cred
                and (old.ssh.port if old.ssh else None) == (ssh["port"] if ssh else None)
                and old.capabilities.diagnostics == frozenset(diagnostics)
                and not old.capabilities.rule_actions and not old.capabilities.agent_actions
            )
            if not same:
                raise AdminConfigConflict("host_id 已存在且内容不同")
        return item, new_target, health

    @staticmethod
    def _fingerprint(revision: str, item: Mapping[str, Any]) -> str:
        # SSH 检查绑定主机、抓取目标、端口及凭据；成功后才能选择诊断白名单。
        # 白名单不改变 SSH 身份，故不参与票据指纹。
        identity = {
            "id": item["id"], "name": item["name"],
            "prometheus": item["prometheus"], "ssh": item.get("ssh"),
        }
        payload = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256((revision + payload).encode()).hexdigest()

    def preview(self, revision: str, draft: Mapping[str, Any]) -> dict[str, Any]:
        manager = self.manager
        if revision != manager.revision():
            raise AdminConfigConflict("配置已变化，请刷新后重试")
        docs = manager._load_docs()
        item, new_target, health = self._prepare(draft, docs)
        if manager.host_registry(docs).get(item["id"]) is None:
            docs["hosts"]["hosts"].append(item)
            manager._validate(docs)
        return {
            "host_id": item["id"], "name": item["name"],
            "job": item["prometheus"]["job"], "instance": item["prometheus"]["instance"],
            "new_target": new_target, "exporter_status": health,
            "ssh_available": "ssh" in item, "diagnostics": item["allow"]["diagnostics"],
            "warning": "已登记后仍需等待 Prometheus 发现；up=0 不阻止保存" if health != "up" else "",
        }

    def ssh_check(self, revision: str, draft: Mapping[str, Any]) -> dict[str, Any]:
        manager = self.manager
        preview = self.preview(revision, draft)
        docs = manager._load_docs()
        item, _new, _health = self._prepare(draft, docs)
        if "ssh" not in item:
            raise AdminConfigError("请先选择可用的 SSH 凭据")
        if manager.host_registry(docs).get(item["id"]) is None:
            docs["hosts"]["hosts"].append(item)
        target = manager.host_registry(docs).require(item["id"]).ssh
        try:
            import paramiko
            client = _new_client(paramiko, target)
            try:
                client.connect(
                    hostname=target.host, port=target.port, username=target.username,
                    key_filename=target.key_filename, password=target.password,
                    timeout=target.connect_timeout,
                    banner_timeout=target.connect_timeout,
                    auth_timeout=target.connect_timeout,
                    channel_timeout=target.connect_timeout,
                    allow_agent=False,
                    look_for_keys=False,
                )
            finally:
                client.close()
        except Exception as exc:
            raise AdminConfigError("SSH 主机密钥或认证检查失败；未执行远程命令") from exc
        ticket = secrets.token_urlsafe(32)
        self._verified[ticket] = (self._fingerprint(revision, item), time.monotonic() + 300)
        return {"verified": True, "verification_id": ticket, "host_id": preview["host_id"]}

    def check_registered_ssh(self, host_id: str) -> dict[str, Any]:
        """显式检查已登记主机的当前连接；不运行命令或修改授权。"""
        host = self.manager.runtime.current.container.hosts().get(host_id)
        if host is None or not host.enabled or host.ssh is None:
            raise AdminConfigError("主机未启用或尚未关联 SSH 凭据")
        target = host.ssh
        try:
            import paramiko
            client = _new_client(paramiko, target)
            try:
                client.connect(hostname=target.host, port=target.port, username=target.username,
                               key_filename=target.key_filename, password=target.password,
                               timeout=target.connect_timeout, banner_timeout=target.connect_timeout,
                               auth_timeout=target.connect_timeout, channel_timeout=target.connect_timeout,
                               allow_agent=False, look_for_keys=False)
            finally:
                client.close()
        except Exception:
            raise AdminConfigError("SSH 主机密钥或认证检查失败；未执行远程命令") from None
        return {"verified": True, "host_id": host_id, "detail": "SSH 认证正常；未执行命令或更改授权"}

    def _target_text(self, address: str) -> str:
        import yaml

        path: Path = self.manager.target_path
        try:
            current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else []
        except Exception as exc:
            raise AdminConfigUnavailable("自管目标文件无效") from exc
        if not isinstance(current, list):
            raise AdminConfigUnavailable("自管目标文件必须是数组")
        existing = {str(value) for group in current if isinstance(group, Mapping)
                    for value in (group.get("targets") or [])}
        if address not in existing:
            current.append({"targets": [address]})
        return yaml.safe_dump(current, allow_unicode=True, sort_keys=False)

    def add(self, revision: str, draft: Mapping[str, Any], actor: str) -> dict[str, Any]:
        manager = self.manager
        with manager._lock:
            docs = manager._load_docs()
            registered = manager.host_registry(docs).get(
                str(draft.get("host_id") or "").strip()
            )
            if registered is not None:
                # 幂等重试不依赖 Prometheus 此刻是否可达，也不重复碰磁盘。
                address = str(draft.get("exporter_address") or "").strip()
                expected_job = _MANAGED_JOB if address else str(draft.get("job") or "").strip()
                expected_instance = address or str(draft.get("instance") or "").strip()
                credential = str(draft.get("credential_id") or "").strip()
                diagnostics = draft.get("diagnostics") or []
                try:
                    port = int(draft.get("ssh_port", 22))
                except (TypeError, ValueError):
                    port = -1
                same = (
                    registered.name == str(draft.get("name") or "").strip()
                    and registered.enabled
                    and registered.monitor.job == expected_job
                    and registered.monitor.instance == expected_instance
                    and registered.credential_ref == credential
                    and (registered.ssh.port if registered.ssh else None)
                    == (port if credential else None)
                    and isinstance(diagnostics, list)
                    and all(isinstance(key, str) for key in diagnostics)
                    and registered.capabilities.diagnostics == frozenset(diagnostics)
                    and not registered.capabilities.rule_actions
                    and not registered.capabilities.agent_actions
                )
                if not same:
                    raise AdminConfigConflict("host_id 已存在且内容不同")
                return {
                    "existing": True, "host_id": registered.host_id,
                    "revision": manager.revision(), "active_revision": manager.active_revision,
                    "exporter_status": "unknown",
                }
            item, new_target, health = self._prepare(draft, docs)
            if revision != manager.revision():
                raise AdminConfigConflict("配置已变化，请刷新后重试")
            if item["allow"]["diagnostics"]:
                ticket = str(draft.get("verification_id") or "")
                verified = self._verified.get(ticket)
                if not verified or verified[0] != self._fingerprint(revision, item) or verified[1] < time.monotonic():
                    raise AdminConfigError("诊断授权必须先通过本次主机的 SSH 连接检查")
            docs["hosts"]["hosts"].append(item)
            manager._validate(docs)
            candidate = manager._candidate(docs)
            if manager.revision() != revision:
                raise AdminConfigConflict("校验期间磁盘配置已变化，请重新加载")
            host_text = manager._dump(docs["hosts"])
            target_path = manager.target_path if new_target else None
            if target_path is not None:
                target_text = self._target_text(item["prometheus"]["instance"])
                target_path.parent.mkdir(parents=True, exist_ok=True)
            if manager.revision() != revision:
                raise AdminConfigConflict("保存前磁盘配置已变化，请重新加载")
            manager.runtime.begin_apply()
            backup_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
            backup_dir = manager.backup_root / backup_id
            old_target = target_path.read_bytes().decode("utf-8") if target_path and target_path.exists() else None
            host_attempted = target_attempted = False
            try:
                backup_dir.mkdir(parents=True, exist_ok=False)
                import shutil
                shutil.copy2(manager.paths["hosts"], backup_dir / manager.paths["hosts"].name)
                if target_path is not None and old_target is not None:
                    shutil.copy2(target_path, backup_dir / ("targets-" + target_path.name))
                host_attempted = True
                manager._atomic_write(manager.paths["hosts"], host_text)
                if target_path is not None:
                    target_attempted = True
                    manager._atomic_write(target_path, target_text)
                new_revision = manager.revision()
            except Exception:
                try:
                    if host_attempted:
                        manager._atomic_write(
                            manager.paths["hosts"],
                            (backup_dir / manager.paths["hosts"].name).read_bytes().decode("utf-8"),
                        )
                    if target_attempted:
                        if old_target is None:
                            target_path.unlink(missing_ok=True)
                        else:
                            manager._atomic_write(target_path, old_target)
                finally:
                    manager.runtime.cancel_apply()
                raise
            from dataclasses import replace
            manager.runtime.commit(replace(candidate, revision=new_revision))
            manager.active_revision = new_revision
            self._verified.pop(str(draft.get("verification_id") or ""), None)
            return {
                "existing": False, "host_id": item["id"], "revision": new_revision,
                "active_revision": new_revision, "backup_id": backup_id,
                "exporter_status": health,
                "message": "已登记、Exporter 不可达" if health != "up" else "已登记并可达",
            }
