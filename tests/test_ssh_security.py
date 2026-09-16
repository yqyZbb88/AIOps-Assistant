"""SSH 主机密钥安全默认值（离线）。"""
from __future__ import annotations

import sys

import pytest

from src.plugins.common.endpoint import SshEndpoint, endpoint_from_params
from src.plugins.common.ssh import SshError, _auth_options, _new_client


class FakeRejectPolicy:
    pass


class FakeClient:
    def __init__(self, *, system_error=None, load_error=None) -> None:
        self.system_keys_loaded = False
        self.loaded_paths: list[str] = []
        self.policy = None
        self.closed = False
        self.system_error = system_error
        self.load_error = load_error

    def load_system_host_keys(self) -> None:
        self.system_keys_loaded = True
        if self.system_error is not None:
            raise self.system_error

    def load_host_keys(self, path: str) -> None:
        self.loaded_paths.append(path)
        if self.load_error is not None:
            raise self.load_error

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def close(self) -> None:
        self.closed = True


def test_endpoint_from_params_keeps_known_hosts():
    endpoint = endpoint_from_params({"host": "h1", "known_hosts": "/etc/ssh/known_hosts"})
    assert endpoint.known_hosts == "/etc/ssh/known_hosts"


def test_new_client_rejects_unknown_hosts(monkeypatch):
    client = FakeClient()
    fake_paramiko = type("FakeParamiko", (), {
        "SSHClient": lambda: client,
        "RejectPolicy": FakeRejectPolicy,
    })
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    out = _new_client(fake_paramiko, SshEndpoint(host="h1", known_hosts="custom_known_hosts"))

    assert out is client
    assert client.system_keys_loaded is True
    assert client.loaded_paths == ["custom_known_hosts"]
    assert isinstance(client.policy, FakeRejectPolicy)


def test_auth_options_disables_agent_for_explicit_password():
    options = _auth_options(SshEndpoint(host="h1", password="secret"))
    assert options == {"allow_agent": False, "look_for_keys": False}


def test_auth_options_disables_agent_for_explicit_private_key():
    options = _auth_options(SshEndpoint(host="h1", key_filename="/tmp/id_ed25519"))
    assert options == {"allow_agent": False, "look_for_keys": False}


def test_auth_options_keeps_agent_only_without_explicit_credentials():
    options = _auth_options(SshEndpoint(host="h1"))
    assert options == {"allow_agent": True, "look_for_keys": True}


def test_new_client_ignores_broken_system_known_hosts(monkeypatch, caplog):
    client = FakeClient(system_error=UnicodeDecodeError("utf-8", b"x", 0, 1, "bad file"))
    fake_paramiko = type("FakeParamiko", (), {
        "SSHClient": lambda: client,
        "RejectPolicy": FakeRejectPolicy,
    })
    monkeypatch.setitem(sys.modules, "paramiko", fake_paramiko)

    with caplog.at_level("WARNING", logger="aiops_assistant.ssh"):
        out = _new_client(fake_paramiko, SshEndpoint(host="h1"))

    assert out is client
    assert "加载系统 known_hosts 失败" in caplog.text


def test_new_client_reports_explicit_known_hosts_failure():
    client = FakeClient(load_error=FileNotFoundError("missing"))
    fake_paramiko = type("FakeParamiko", (), {
        "SSHClient": lambda: client,
        "RejectPolicy": FakeRejectPolicy,
    })

    with pytest.raises(SshError) as exc_info:
        _new_client(fake_paramiko, SshEndpoint(host="h1", known_hosts="/missing/known_hosts"))

    assert "/missing/known_hosts" in str(exc_info.value)
    assert "FileNotFoundError" in str(exc_info.value)
