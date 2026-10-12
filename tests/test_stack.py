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


def test_migration_requires_stopped_stack_and_uses_container(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import sys
    monkeypatch.setattr(stack, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["stack.py", "migrate-config", "--dry-run"])
    commands = []
    monkeypatch.setattr(stack.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(stack, "compose", lambda *a: commands.append(a) or 0)
    assert stack.main() == 0
    assert commands[0] == ("build", "init")
    assert commands[1] == ("run", "--rm", "--no-deps", "-v", str(tmp_path) + ":/migration-project", "init", "migrate-config", "--dry-run")


def test_migration_refuses_running_services(monkeypatch, capsys):
    from types import SimpleNamespace
    import sys
    monkeypatch.setattr(sys, "argv", ["stack.py", "migrate-config"])
    monkeypatch.setattr(stack.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="running-container"))
    monkeypatch.setattr(stack, "compose", lambda *a: (_ for _ in ()).throw(AssertionError("不得开始迁移")))
    assert stack.main() == 1
    assert "停止" in capsys.readouterr().err
