"""禁网容器内的真实 Linux shell/pkill 验收；不启动 SSH、不连接真实主机。

sudo 使用无提权包装替身，保留其 argv 和等待子进程的行为。只能在独立 PID
命名空间的临时容器内运行，匹配目标由本脚本创建并清理。
"""
from __future__ import annotations

import io
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace

# 标准库验收镜像没有监控/模型依赖，只加载实际 SSH 内核文件；不运行插件总注册。
source_root = Path(importlib.util.find_spec("src").origin).parent
for package, path in (("src.plugins", source_root / "plugins"),
                      ("src.plugins.common", source_root / "plugins" / "common")):
    module = ModuleType(package)
    module.__path__ = [str(path)]
    sys.modules[package] = module

from src.plugins.common.endpoint import SshEndpoint
from src.plugins.common.ssh import exec_command
from tests.ssh_script_cases import compatibility_cases


class ProcessChannel:
    def __init__(self, process, stdin):
        self.process = process
        self.stdin = stdin
        self.out = []
        self.err = []

    def shutdown_write(self):
        out, err = self.process.communicate(self.stdin.getvalue(), timeout=5)
        self.out = [out] if out else []
        self.err = [err] if err else []

    def recv_ready(self):
        return bool(self.out)

    def recv_stderr_ready(self):
        return bool(self.err)

    def recv(self, _size):
        return self.out.pop(0)

    def recv_stderr(self, _size):
        return self.err.pop(0)

    def exit_status_ready(self):
        return self.process.poll() is not None

    def recv_exit_status(self):
        return self.process.returncode if self.process.returncode >= 0 else -1

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.communicate(timeout=5)


def main():
    if not Path("/.dockerenv").exists() or not Path("/proc/self/cmdline").exists():
        raise SystemExit("只能在独立临时 Linux Docker 容器中验收")
    if not shutil.which("pkill"):
        raise SystemExit("验收镜像必须提供真实 procps pkill")

    checks = {}
    with tempfile.TemporaryDirectory(prefix="ssh-outcome-") as directory:
        root = Path(directory)
        shim = root / "sudo"
        shim.write_text('#!/bin/sh\n"$@"\nrc=$?\nexit "$rc"\n')
        shim.chmod(0o700)
        env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}")
        marker = root.name + "-load_gen.sh"
        target_path = root / marker
        target_path.write_text("while :; do sleep 0.1; done\n")
        # 唯一模式仅匹配本次容器生成的目标，不指向任何真实任务。
        command = f"pkill -9 -f {shlex.quote(marker)}"
        targets = []
        clients = []

        def start_target():
            target = subprocess.Popen(["sh", str(target_path)])
            targets.append(target)
            return target

        class LocalClient:
            def load_system_host_keys(self):
                pass

            def set_missing_host_key_policy(self, policy):
                pass

            def connect(self, **kwargs):
                assert kwargs["hostname"] == "synthetic-local-only"

            def exec_command(self, command, **kwargs):
                process = subprocess.Popen(["bash", "-c", command], env=env,
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, start_new_session=True)
                stdin = io.BytesIO()
                self.channel = ProcessChannel(process, stdin)
                stdin.channel = self.channel
                return stdin, SimpleNamespace(channel=self.channel), None

            def close(self):
                if hasattr(self, "channel"):
                    self.channel.close()

        def new_client():
            client = LocalClient()
            clients.append(client)
            return client

        sys.modules["paramiko"] = SimpleNamespace(SSHClient=new_client, RejectPolicy=object)
        try:
            target = start_target()
            old = subprocess.run(["bash", "-c", f"sudo sh -c {shlex.quote(command)}"],
                                 env=env, capture_output=True, timeout=5, start_new_session=True)
            assert target.wait(timeout=5) == -9
            assert old.returncode == -9, old.returncode
            checks["old_wrapper_killed_with_target"] = True

            target = start_target()
            result = exec_command(SshEndpoint(host="synthetic-local-only"), command, sudo=True)
            assert target.wait(timeout=5) == -9
            assert result.exit_code == 0 and result.stdout == result.stderr == ""
            checks["stdin_dispatch_kills_target_and_returns_zero"] = True

            result = exec_command(SshEndpoint(host="synthetic-local-only"), command, sudo=True)
            assert result.exit_code == 1 and result.stdout == result.stderr == ""
            checks["no_match_preserves_real_exit_one"] = True

            quoted = "printf '%s\\n' '含中文与 \" 引号'\nprintf '%s' error >&2\nexit 7"
            result = exec_command(SshEndpoint(host="synthetic-local-only"), quoted, sudo=True)
            assert (result.exit_code, result.stdout, result.stderr) == (7, '含中文与 " 引号', "error")
            checks["quotes_unicode_multiline_stdout_stderr_and_exit_preserved"] = True

            # 与原 sh -c 在非交互 EOF 下逐项对照；不运行真实 systemctl/清理脚本。
            for name, script in compatibility_cases().items():
                original = subprocess.run(["sh", "-c", script], input=b"", env=env,
                                          capture_output=True, timeout=5)
                current = exec_command(SshEndpoint(host="synthetic-local-only"), script, sudo=True)
                expected = (original.returncode, original.stdout.decode().strip(), original.stderr.decode().strip())
                actual = (current.exit_code, current.stdout, current.stderr)
                # 语法错误/找不到命令的诊断前缀可包含 eval 上下文，只比较分类及诊断存在。
                if name in {"syntax_error", "missing_command"}:
                    assert actual[0:2] == expected[0:2] and bool(actual[2]) == bool(expected[2]), (name, actual, expected)
                else:
                    assert actual == expected, (name, actual, expected)
                checks[f"compatibility_{name}"] = True

            # 加载器失败必须拒绝执行部分脚本，不能让 eval 的空结果掩盖读取失败。
            broken_cat = root / "cat"
            broken_cat.write_text("#!/bin/sh\nprintf 'printf UNEXPECTED_EXECUTION'\nprintf 'synthetic read failure' >&2\nexit 23\n")
            broken_cat.chmod(0o700)
            current = exec_command(SshEndpoint(host="synthetic-local-only"), "printf intended", sudo=True)
            assert (current.exit_code, current.stdout, current.stderr) == (23, "", "synthetic read failure")
            checks["loader_failure_refuses_partial_script"] = True
        finally:
            for target in targets:
                if target.poll() is None:
                    target.kill()
                target.wait(timeout=5)
            for client in clients:
                client.close()
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
