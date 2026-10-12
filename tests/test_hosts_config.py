from __future__ import annotations

import pytest

from src.interfaces.host import HostConfigError, HostError
from src.utils.hosts_config import load_host_registry, parse_hosts


SAMPLE = """
version: 1
credentials:
  ops:
    username: ops
    password: secret
hosts:
  - id: host-a
    name: 应用主机
    enabled: true
    prometheus: {job: node, instance: "a:9100"}
    ssh: {host: "10.0.0.1", credential_ref: ops}
    allow:
      diagnostics: [memory]
      rule_actions: []
      agent_actions: [restart_service]
  - id: host-b
    name: 应用主机
    enabled: true
    prometheus: {job: node, instance: "b:9100"}
    allow: {}
"""


def test_parse_two_hosts_and_hide_credentials():
    registry = parse_hosts(SAMPLE)
    assert [host.host_id for host in registry.enabled()] == ["host-a", "host-b"]
    assert registry.require("host-a").ssh.host == "10.0.0.1"
    public = registry.require("host-a").to_public_dict()
    assert public["ssh_configured"] is True
    assert "secret" not in repr(public) and "10.0.0.1" not in repr(public)


def test_name_ambiguity_requires_host_id():
    with pytest.raises(HostError, match="歧义"):
        parse_hosts(SAMPLE).resolve_name("应用主机")


def test_alert_mapping_is_exact_and_unknown_is_not_defaulted():
    registry = parse_hosts(SAMPLE)
    labels = {"job": "node", "instance": "a:9100"}
    assert registry.resolve_labels(labels).host_id == "host-a"
    assert registry.resolve_labels({"instance": "missing:9100"}).host is None


@pytest.mark.parametrize(
    "text, message",
    [
        (SAMPLE.replace('"b:9100"', '"a:9100"'), "instance 重复"),
        (SAMPLE.replace("credential_ref: ops", "credential_ref: missing"), "未知 SSH credential"),
        (SAMPLE.replace("allow: {}", "allow: {diagnostics: [memory]}"), "未配置 SSH"),
        (SAMPLE.replace("id: host-a", "id: Legacy_Default"), "非法或保留"),
    ],
)
def test_invalid_host_configs_are_rejected(text, message):
    with pytest.raises(HostConfigError, match=message):
        parse_hosts(text)


def test_missing_host_file_never_creates_a_default_target(monkeypatch):
    monkeypatch.delenv("OPS_HOSTS_FILE")
    with pytest.raises(HostConfigError, match="hosts.file"):
        load_host_registry(None)


@pytest.mark.parametrize("password", ["  test-password  ", "Ｔｅｓｔ①", "  "])
def test_password_is_preserved_exactly(password, monkeypatch):
    monkeypatch.setenv("TEST_SSH_PASSWORD", password)
    text = SAMPLE.replace("password: secret", 'password: "${TEST_SSH_PASSWORD}"')
    assert parse_hosts(text).require("host-a").ssh.password == password


@pytest.mark.parametrize("password", ["", None])
def test_empty_password_is_unset(password):
    import yaml
    data = yaml.safe_load(SAMPLE)
    data["credentials"]["ops"]["password"] = password
    assert parse_hosts(yaml.safe_dump(data)).require("host-a").ssh.password is None


@pytest.mark.parametrize("password", ['test"quote', 'test\\path', "test\nline"])
def test_environment_password_with_yaml_syntax_is_preserved(password, monkeypatch):
    monkeypatch.setenv("TEST_SSH_PASSWORD", password)
    text = SAMPLE.replace("password: secret", 'password: "${TEST_SSH_PASSWORD}"')
    assert parse_hosts(text).require("host-a").ssh.password == password
