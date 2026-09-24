"""用 Node 内置测试工具验证无第三方依赖的控制台交互逻辑。"""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_console_ui_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 未安装，前端运行时测试不可用")
    script = Path(__file__).with_name("console_ui.test.cjs")
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr
