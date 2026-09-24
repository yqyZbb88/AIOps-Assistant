"""写命令目录：解析/校验（纯）+ 通用执行器 CatalogCommandExecutor（monkeypatch exec_command）。

全部离线：不触发真实 SSH / paramiko / langchain。
"""
from __future__ import annotations

import logging

import pytest

from src.interfaces.action import ActionError
from src.plugins.actions.catalog import (
    ActionsConfigError,
    load_actions_catalog,
    parse_actions,
)
from src.plugins.actions.executor import CatalogCommandExecutor, build_catalog_executors
from src.plugins.common.ssh import SshError

# 一条目录样例：密码仅测试内联（真实约定是 .env 占位）
SAMPLE = """
enabled: true
commands:
  restart_service:
    summary: 重启服务
    template: "systemctl restart {service}"
    sudo: false
    params:
      service:
        type: str
        pattern: "^[A-Za-z0-9_.:@-]{1,128}$"
        description: 服务名
  cleanup_disk:
    summary: 跑清理脚本（无参命令，模板即整条）
    template: "cd /opt/ops && bash cleanup_disk.sh"
    sudo: true
    params: {}
"""


def _cmd(cfg, key):
    return next(c for c in cfg.commands if c.key == key)


# ---------------------------------------------------------------------------
# 目录解析 + 校验
# ---------------------------------------------------------------------------
class TestCatalogParse:
    def test_valid_sample(self):
        cfg = parse_actions(SAMPLE)
        assert [c.key for c in cfg.commands] == ["restart_service", "cleanup_disk"]
        svc = _cmd(cfg, "restart_service")
        assert svc.sudo is False
        (p,) = svc.params
        assert p.name == "service" and p.required and p.pattern
        assert _cmd(cfg, "cleanup_disk").sudo is True  # 写命令允许声明 sudo
        assert _cmd(cfg, "cleanup_disk").params == ()

    def test_unknown_placeholder_rejected(self):
        text = SAMPLE.replace('"systemctl restart {service}"', '"echo {oops} {service}"')
        with pytest.raises(ActionsConfigError, match="未声明"):
            parse_actions(text)

    def test_str_param_without_pattern_rejected(self):
        text = SAMPLE.replace('pattern: "^[A-Za-z0-9_.:@-]{1,128}$"', "pattern:")
        with pytest.raises(ActionsConfigError, match="pattern"):
            parse_actions(text)

    def test_bad_command_key_rejected(self):
        text = SAMPLE.replace("  restart_service:", "  1restart_service:")
        with pytest.raises(ActionsConfigError, match="命令名"):
            parse_actions(text)

    def test_disabled_command_is_valid_but_not_registered(self):
        cfg = parse_actions(SAMPLE.replace("  restart_service:\n", "  restart_service:\n    enabled: false\n"))
        assert [command.key for command in cfg.commands] == ["cleanup_disk"]

    def test_repo_default_catalog_loads_recommended_commands(self):
        cfg = load_actions_catalog()  # config/actions.yaml
        assert cfg is not None
        assert [c.key for c in cfg.commands] == [
            "restart_service", "cleanup_disk",
        ]
        sudo_by_key = {c.key: c.sudo for c in cfg.commands}
        assert sudo_by_key == {"restart_service": True, "cleanup_disk": False}

    def test_build_catalog_executors_keys(self):
        executors = build_catalog_executors(parse_actions(SAMPLE))
        assert sorted(executors) == ["cleanup_disk", "restart_service"]
        assert isinstance(executors["restart_service"], CatalogCommandExecutor)


# ---------------------------------------------------------------------------
# 通用执行器（monkeypatch 共享内核 exec_command）
# ---------------------------------------------------------------------------
@pytest.fixture()
def executor():
    cfg = parse_actions(SAMPLE)
    return CatalogCommandExecutor(_cmd(cfg, "restart_service"))


class TestCatalogExecutor:
    def test_execute_ok_builds_and_audits(self, executor, monkeypatch, caplog):
        seen = {}

        def fake(endpoint, command, *, sudo, output_limit):
            seen["host"] = endpoint.host
            seen["cmd"] = command
            seen["sudo"] = sudo
            seen["output_limit"] = output_limit
            return (0, "restarted ok", "")

        monkeypatch.setattr("src.plugins.common.ssh.exec_command", fake)
        with caplog.at_level(logging.INFO, logger="aiops_assistant.audit"):
            result = executor.execute({
                "host": "192.168.1.1", "port": 22, "username": "root",
                "password": "secret-in-test", "service": "nginx",
            })
        assert seen["host"] == "192.168.1.1"
        assert seen["cmd"] == "systemctl restart nginx"
        assert seen["sudo"] is False  # sudo 只按目录项声明
        assert result["ok"] is True and result["exit_code"] == 0
        # 审计：不含密码值、不含整段输出
        assert "secret-in-test" not in caplog.text
        assert "restarted ok" not in caplog.text

    def test_execute_nonzero_raises_actionerror(self, executor, monkeypatch):
        monkeypatch.setattr(
            "src.plugins.common.ssh.exec_command",
            lambda *a, **k: (1, "", "Unit nginx not found"),
        )
        with pytest.raises(ActionError, match="rc=1"):
            executor.execute({"host": "h", "service": "nginx"})

    def test_execute_ssh_error_raises_actionerror(self, executor, monkeypatch):
        def boom(*a, **k):
            raise SshError("SSH 连接 root@h:22 失败: auth")

        monkeypatch.setattr("src.plugins.common.ssh.exec_command", boom)
        with pytest.raises(ActionError, match="SSH 连接"):
            executor.execute({"host": "h", "service": "nginx"})

    def test_execute_invalid_param_rejected_no_ssh(self, executor, monkeypatch):
        def should_not_call(*a, **k):
            raise AssertionError("非法参数不应进入 SSH 路径")

        monkeypatch.setattr("src.plugins.common.ssh.exec_command", should_not_call)
        with pytest.raises(ActionError, match="参数不合法"):
            executor.execute({"host": "h", "service": "nginx; rm -rf /"})

    def test_execute_missing_host_rejected(self, executor, monkeypatch):
        def should_not_call(*a, **k):
            raise AssertionError("缺 host 不应进入 SSH 路径")

        monkeypatch.setattr("src.plugins.common.ssh.exec_command", should_not_call)
        with pytest.raises(ActionError, match="缺少必填连接参数 host"):
            executor.execute({"service": "nginx"})

    def test_execute_rejects_extra_param(self, executor, monkeypatch):
        monkeypatch.setattr(
            "src.plugins.common.ssh.exec_command",
            lambda *a, **k: (0, "", ""),
        )
        # sudo/workdir 等不属于目录声明的参数 → 拒绝（目录只认声明参数）
        with pytest.raises(ActionError, match="不接受参数"):
            executor.execute({"host": "h", "service": "nginx", "sudo": False})
