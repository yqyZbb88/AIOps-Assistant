"""离线 Bash 安全边界入口；压测工具本身不依赖 Python。"""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("bash") is None, reason="需要 Linux Bash")
def test_bash_stress_safety_boundaries():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(root / "tests/safe_stress.test.sh")],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Bash 检查通过" in result.stdout
