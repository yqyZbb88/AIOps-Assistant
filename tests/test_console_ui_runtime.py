"""用 Node 内置测试工具验证控制台交互与本地 Markdown 解析器。"""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_console_ui_runtime():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js 未安装，前端运行时测试不可用")
    scripts = [Path(__file__).with_name(name) for name in ("console_ui.test.cjs", "console_markdown.test.cjs")]
    result = subprocess.run(
        [node, "--test", *map(str, scripts)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stdout + result.stderr



def test_console_metric_units_match_backend_catalog():
    import re
    from src.metrics.catalog import METRIC_CATALOG
    source = (Path(__file__).resolve().parents[1] / "src/web/console.js").read_text(encoding="utf-8")
    source = source.split("const METRICS = [", 1)[1].split("const METRIC_MAP", 1)[0]
    entries = re.findall(r'\["([^"\n]+)",\s*"([^"\n]+)",\s*"([^"\n]*)"\]', source)
    units = {metric_id: unit for metric_id, _, unit in entries}
    aliases = {"B": "bytes", "s": "seconds"}
    for metric in METRIC_CATALOG:
        assert aliases.get(units[metric.metric_id], units[metric.metric_id]) == metric.unit, metric.metric_id
