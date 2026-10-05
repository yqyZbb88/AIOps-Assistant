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


def test_compose_uses_explicit_project_file_and_has_no_host_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(dev_stack, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(dev_stack.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or type("Result", (), {"returncode": 0})())
    assert dev_stack.compose("ps") == 0
    assert calls[0][0] == ["docker", "compose", "--project-directory", str(tmp_path), "-f", str(tmp_path / "compose.yaml"), "ps"]
    assert not (tmp_path / ".runtime").exists()
