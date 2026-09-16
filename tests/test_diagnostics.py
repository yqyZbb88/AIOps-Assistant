"""只读诊断：配置解析/校验/命令构建（纯）、SSH 插件 run（monkeypatch）、渲染与注册。

全部离线：不触发真实 SSH / paramiko / langchain。
"""
from __future__ import annotations

import logging

import pytest

from src.interfaces.diagnostic import DiagnosticError, render_diag
from src.plugins.diagnostics import commands as diag_commands
from src.plugins.diagnostics.commands import (
    DiagnosticsConfigError,
    build_command,
    load_diagnostics_config,
    parse_diagnostics,
    validated_params,
)
from src.plugins.diagnostics.ssh_diagnostics import SshDiagnostics
from src.plugins.common.ssh import SshError
from src.registry import registry

# 一条带输出截断上限(20)的多命令配置；密码仅测试内联（真实约定是 .env 占位）
SAMPLE = """
enabled: true
provider: ssh
target:
  host: 192.168.1.1
  port: 22
  username: root
  password: "secret-in-test"
  connect_timeout: 5
output_limit: 20
commands:
  memory:
    summary: 查看内存明细
    template: "free -m"
    params: {}
  service_status:
    summary: 查看服务状态
    template: "systemctl status --no-pager -l {service}"
    params:
      service:
        type: str
        pattern: "^[A-Za-z0-9_.:@-]{1,128}$"
        description: 服务名
  service_logs:
    summary: 查看服务日志
    template: "journalctl -u {service} --no-pager -n {lines}"
    params:
      service:
        type: str
        pattern: "^[A-Za-z0-9_.:@-]{1,128}$"
        description: 服务名
      lines:
        type: int
        default: 50
        min: 1
        max: 200
        description: 行数
"""


def _cmd(config, key):
    return next(c for c in config.commands if c.key == key)


# ---------------------------------------------------------------------------
# 配置解析 + 结构校验（纯）
# ---------------------------------------------------------------------------
class TestParse:
    def test_valid_sample_config(self):
        cfg = parse_diagnostics(SAMPLE)
        assert [c.key for c in cfg.commands] == [
            "memory", "service_status", "service_logs",
        ]
        assert cfg.target.host == "192.168.1.1"
        assert cfg.output_limit == 20
        status = _cmd(cfg, "service_status")
        (svc,) = status.params
        assert svc.name == "service" and svc.required and svc.pattern
        logs = _cmd(cfg, "service_logs")
        by_name = {p.name: p for p in logs.params}
        assert by_name["lines"].maximum == 200 and by_name["lines"].default == 50

    def test_unknown_placeholder_rejected(self):
        text = SAMPLE.replace(
            'template: "systemctl status --no-pager -l {service}"',
            'template: "echo {oops} {service}"',
        )
        with pytest.raises(DiagnosticsConfigError, match="未声明"):
            parse_diagnostics(text)

    def test_str_param_without_pattern_rejected(self):
        text = SAMPLE.replace('pattern: "^[A-Za-z0-9_.:@-]{1,128}$"', "pattern:")
        with pytest.raises(DiagnosticsConfigError, match="pattern"):
            parse_diagnostics(text)

    def test_declared_but_unused_param_rejected(self):
        text = SAMPLE.replace(
            'template: "free -m"\n    params: {}',
            'template: "free -m"\n    params:\n      x:\n        type: str\n'
            '        pattern: "^[a-z]+$"',
        )
        with pytest.raises(DiagnosticsConfigError, match="多余"):
            parse_diagnostics(text)

    def test_bad_command_key_rejected(self):
        text = SAMPLE.replace("  memory:\n", "  1memory:\n")
        with pytest.raises(DiagnosticsConfigError, match="命令名"):
            parse_diagnostics(text)

    def test_repo_default_config_loads_five_commands(self):
        cfg = load_diagnostics_config()  # config/diagnostics.yaml
        assert cfg is not None
        keys = [c.key for c in cfg.commands]
        assert keys == [
            "memory", "disk", "top_processes", "service_status", "service_logs",
        ]


# ---------------------------------------------------------------------------
# 参数校验 + 命令构建（安全边界）
# ---------------------------------------------------------------------------
class TestBuildCommand:
    @pytest.fixture()
    def cfg(self):
        return parse_diagnostics(SAMPLE)

    def test_build_exact_command_int_unquoted(self, cfg):
        cmd = build_command(_cmd(cfg, "service_logs"), {"service": "nginx", "lines": 50})
        assert cmd == "journalctl -u nginx --no-pager -n 50"

    def test_int_out_of_range_clamped(self, cfg):
        cmd = build_command(_cmd(cfg, "service_logs"), {"service": "nginx", "lines": 9999})
        assert cmd.endswith(" -n 200")

    def test_non_int_rejected(self, cfg):
        with pytest.raises(DiagnosticError, match="整数"):
            build_command(_cmd(cfg, "service_logs"), {"service": "nginx", "lines": "abc"})

    def test_injection_str_rejected(self, cfg):
        with pytest.raises(DiagnosticError, match="非法字符"):
            build_command(_cmd(cfg, "service_status"), {"service": "nginx; rm -rf /"})

    def test_missing_required_rejected(self, cfg):
        with pytest.raises(DiagnosticError, match="必填参数"):
            validated_params(_cmd(cfg, "service_status"), {})

    def test_extra_param_rejected(self, cfg):
        with pytest.raises(DiagnosticError, match="不接受参数"):
            build_command(_cmd(cfg, "service_status"), {"service": "nginx", "host": "x"})

    def test_str_value_is_shell_quoted(self):
        # 允许空格的 pattern：证明动态值经过 shlex.quote，而非裸拼
        text = """
enabled: true
target:
  host: 192.168.1.1
commands:
  run:
    summary: 测试回显
    template: "echo {x}"
    params:
      x:
        type: str
        pattern: "^[A-Za-z0-9 _-]{1,64}$"
"""
        cmd = build_command(_cmd(parse_diagnostics(text), "run"), {"x": "hello world"})
        assert cmd == "echo 'hello world'"


# ---------------------------------------------------------------------------
# SshDiagnostics.run（monkeypatch 共享内核 exec_command）
# ---------------------------------------------------------------------------
@pytest.fixture()
def diag():
    return SshDiagnostics(parse_diagnostics(SAMPLE))


class TestSshRun:
    def test_run_ok_audits_and_truncates(self, diag, monkeypatch, caplog):
        seen = {}

        def fake_ssh(endpoint, command, *, sudo):
            seen["host"] = endpoint.host
            seen["cmd"] = command
            assert sudo is False  # 只读诊断恒不 sudo
            return (0, "MEM-LINE " + "x" * 60, "")

        monkeypatch.setattr("src.plugins.common.ssh.exec_command", fake_ssh)
        with caplog.at_level(logging.INFO, logger="aiops_assistant.audit"):
            result = diag.run("memory")
        assert seen["host"] == "192.168.1.1"
        assert seen["cmd"] == "free -m"
        assert result["exit_code"] == 0 and result["ok"] is True
        assert result["output"].startswith("MEM-LINE ")
        assert "截断" in result["output"]
        assert result["command"] == "memory"

        # 审计：含命令/主机，不含密码
        assert '"command": "memory"' in caplog.text
        assert '"host": "192.168.1.1"' in caplog.text
        assert "secret-in-test" not in caplog.text
        assert '"password"' not in caplog.text

    def test_run_nonzero_exit_not_raised(self, diag, monkeypatch):
        monkeypatch.setattr(
            "src.plugins.common.ssh.exec_command",
            lambda *a, **k: (1, "", "Unit not found"),
        )
        result = diag.run("memory")
        assert result["ok"] is False and result["exit_code"] == 1
        assert "Unit not found" in result["output"]

    def test_run_ssh_error_raises_and_audits_error(self, diag, monkeypatch, caplog):
        def boom(*a, **k):
            raise SshError("SSH 连接 192.168.1.1 失败: auth")

        monkeypatch.setattr("src.plugins.common.ssh.exec_command", boom)
        with pytest.raises(DiagnosticError, match="SSH 诊断执行失败"):
            with caplog.at_level(logging.WARNING, logger="aiops_assistant.audit"):
                diag.run("memory")
        assert "auth" in caplog.text or "error" in caplog.text

    def test_unknown_command_rejected(self, diag):
        with pytest.raises(DiagnosticError, match="未知诊断命令"):
            diag.run("definitely_not_whitelisted")

    def test_validated_params_defaults_used(self, diag):
        values = validated_params(_cmd(diag.config, "service_logs"), {"service": "nginx"})
        assert values["lines"] == 50  # 缺省时用默认


# ---------------------------------------------------------------------------
# 渲染 + 注册
# ---------------------------------------------------------------------------
class TestMisc:
    def test_render_diag_ok(self):
        text = render_diag({"ok": True, "host": "h", "command": "memory",
                            "exit_code": 0, "output": "total 1024"})
        assert "exit_code=0" in text and "```" in text and "total 1024" in text

    def test_render_diag_nonzero(self):
        text = render_diag({"ok": False, "host": "h", "command": "x",
                            "exit_code": 3, "output": "inactive"})
        assert "非零退出码 3" in text

    def test_render_diag_error(self):
        text = render_diag({"error": "连不上"})
        assert "执行失败" in text and "连不上" in text

    def test_registry_has_ssh_diagnostics(self):
        from src import plugins  # noqa: F401  导入即注册

        assert registry.has_diagnostic("ssh")
        assert "ssh" in registry.diagnostic_names()
