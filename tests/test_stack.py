"""Compose 辅助入口只操作容器，不创建宿主机运行环境。"""
from pathlib import Path
from scripts import stack


def test_compose_uses_explicit_project_file_and_has_no_host_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(stack, "ROOT", tmp_path)
    calls = []
    monkeypatch.setattr(stack.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)) or type("Result", (), {"returncode": 0})())
    assert stack.compose("ps") == 0
    assert calls[0][0] == ["docker", "compose", "--project-directory", str(tmp_path), "-f", str(tmp_path / "compose.yaml"), "ps"]
    assert not (tmp_path / ".runtime").exists()
