"""SSH 主机密钥安全默认值（离线）。"""
from __future__ import annotations

import sys

from src.plugins.common.endpoint import SshEndpoint, endpoint_from_params
from src.plugins.common.ssh import _new_client


class FakeRejectPolicy:
    pass


class FakeClient:
    def __init__(self) -> None:
        self.system_keys_loaded = False
        self.loaded_paths: list[str] = []
        self.policy = None
        self.closed = False

    def load_system_host_keys(self) -> None:
        self.system_keys_loaded = True

    def load_host_keys(self, path: str) -> None:
        self.loaded_paths.append(path)

    def set_missing_host_key_policy(self, policy) -> None:
        self.policy = policy

    def close(self) -> None:
        self.closed = True


def test_endpoint_from_params_keeps_known_hosts():
    endpoint = endpoint_from_params({"host": "h1", "known_hosts": "/etc/ssh/known_hosts"})
    assert endpoint.known_hosts == "/etc/ssh/known_hosts"


def test_connect_timeout_default_has_a_single_source():
    """同一套 SSH 层只能有一个「没写就用多少」的答案。

    历史遗留：SshEndpoint / endpoint_from_params（规则写命令那条路）是 15，而诊断与
    Agent 写命令的配置模型是 8——同一个概念三条路径两个值，运维没法预期，也没人知道
    哪个才是「本项目的默认」。现在统一由 common/endpoint.py 定义，两边都引用它。
    """
    from src.plugins.agent_actions.catalog import AgentActionsTarget
    from src.plugins.common.endpoint import DEFAULT_CONNECT_TIMEOUT
    from src.plugins.diagnostics.commands import DiagnosticsTarget

    assert SshEndpoint(host="h1").connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert endpoint_from_params({"host": "h1"}).connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert DiagnosticsTarget().connect_timeout == DEFAULT_CONNECT_TIMEOUT
    assert AgentActionsTarget().connect_timeout == DEFAULT_CONNECT_TIMEOUT
    # 这个值同时是 ssh.py 的连接与命令超时上限，改它等于改所有 SSH 路径的行为；
    # 真要改，得连同 config/*.yaml 与 docs/configuration.md 一起改。
    assert DEFAULT_CONNECT_TIMEOUT == 8


def test_endpoint_params_can_override_connect_timeout():
    """默认值统一了，但逐条规则的显式声明仍然说话（写命令要更长就自己写）。"""
    assert endpoint_from_params({"host": "h1", "connect_timeout": 30}).connect_timeout == 30
    assert endpoint_from_params({"host": "h1", "connect_timeout": 0}).connect_timeout == 8, \
        "0 在 paramiko 里是「永不超时」，不能让它把默认值顶掉"


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
