"""开发栈与本地 Python 服务的 Token 来源不能混淆。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import dev_stack


def test_local_admin_token_uses_local_env_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dev_stack, "ROOT", tmp_path)
    monkeypatch.delenv("OPS_API_TOKENS", raising=False)
    monkeypatch.delenv("OPS_ADMIN_IDENTITIES", raising=False)
    (tmp_path / ".env").write_text(
        "OPS_API_TOKENS=" + json.dumps({"local-admin": "admin", "local-user": "alice"}) + "\n"
        'OPS_ADMIN_IDENTITIES=["admin"]\n',
        encoding="utf-8",
    )
    assert dev_stack.local_admin_token() == "local-admin"


def test_local_admin_token_respects_process_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dev_stack, "ROOT", tmp_path)
    (tmp_path / ".env").write_text('OPS_API_TOKENS={"old":"admin"}\n', encoding="utf-8")
    monkeypatch.setenv("OPS_API_TOKENS", '{"current":"operator"}')
    monkeypatch.setenv("OPS_ADMIN_IDENTITIES", '["operator"]')
    assert dev_stack.local_admin_token() == "current"


def test_local_admin_token_rejects_missing_admin_mapping(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dev_stack, "ROOT", tmp_path)
    monkeypatch.delenv("OPS_API_TOKENS", raising=False)
    monkeypatch.delenv("OPS_ADMIN_IDENTITIES", raising=False)
    (tmp_path / ".env").write_text('OPS_API_TOKENS={"only-user":"alice"}\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="没有映射到管理员身份"):
        dev_stack.local_admin_token()


def test_compose_uses_runtime_token_even_if_shell_has_another_token(tmp_path: Path, monkeypatch):
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    env_file = runtime / "compose.env"
    env_file.write_text(
        'OPS_API_TOKENS={"compose-admin":"admin"}\n'
        'OPS_APPROVER_IDENTITIES=["admin"]\n'
        "AIOPS_USER_TOKEN=compose-admin\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(dev_stack, "ROOT", tmp_path)
    monkeypatch.setattr(dev_stack, "RUNTIME", runtime)
    monkeypatch.setattr(dev_stack, "ENV_FILE", env_file)
    monkeypatch.setenv("OPS_API_TOKENS", '{"shell-admin":"admin"}')
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(dev_stack.subprocess, "run", fake_run)
    assert dev_stack.compose("ps") == 0
    assert calls[0][1]["env"]["OPS_API_TOKENS"] == '{"compose-admin":"admin"}'
