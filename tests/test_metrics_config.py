"""metrics.yaml：代码指标目录与运行配置的严格边界。"""
from __future__ import annotations

import pytest

from src.plugins.monitors.prometheus_catalog import METRIC_CATALOG
from src.utils.config_loader import ConfigError
from src.utils.metrics_config import (
    default_metrics_config,
    load_metrics_config,
    parse_metrics_config,
)


def _config(metric_block: str) -> str:
    return f"""version: 1
instance: 192.168.1.10:9100
metrics:
{metric_block}
"""


def test_catalog_has_seventeen_unique_metrics():
    ids = [definition.metric_id for definition in METRIC_CATALOG]
    assert len(ids) == 17
    assert len(ids) == len(set(ids))
    assert "cpu.load1" in ids
    assert "network.rx_errors" in ids


def test_default_config_preserves_existing_four():
    config = default_metrics_config()
    assert len(config.metrics) == 17
    assert config.metrics["cpu.usage"].surfaces.chat is True
    assert config.metrics["memory.usage"].surfaces.alert_analysis is True
    assert config.metrics["disk.usage"].severity_enabled is True
    assert config.metrics["cpu.load1"].severity_enabled is False


def test_parse_selector_and_surfaces():
    config = parse_metrics_config(
        _config("""  cpu.usage:
    enabled: true
    surfaces: { inspection: true, chat: false, alert_analysis: true }
    warning: 80
    critical: 90
    severity_enabled: true
    selector:
      instance: h:9100
      mode=~: user
""")
    )
    runtime = config.metrics["cpu.usage"]
    assert runtime.selector == {"instance": "h:9100", "mode=~": "user"}
    assert runtime.surfaces.alert_analysis is True


def test_unknown_metric_is_rejected():
    with pytest.raises(ConfigError, match="未知指标: cpu.nope"):
        parse_metrics_config(_config("""  cpu.nope:
    enabled: true
"""))


def test_unknown_field_is_rejected():
    with pytest.raises(ConfigError, match="未知字段: surprise"):
        parse_metrics_config(_config("""  cpu.usage:
    enabled: true
    surprise: true
"""))


def test_observe_metric_rejects_severity():
    with pytest.raises(ConfigError, match="observe 指标"):
        parse_metrics_config(_config("""  disk.read_bytes:
    enabled: true
    surfaces: { inspection: true }
    warning: 1
    critical: 2
    severity_enabled: true
"""))


def test_above_threshold_order_is_enforced():
    with pytest.raises(ConfigError, match="warning < critical"):
        parse_metrics_config(_config("""  cpu.usage:
    surfaces: { inspection: true }
    warning: 90
    critical: 80
    severity_enabled: true
"""))


def test_below_threshold_order_is_enforced():
    with pytest.raises(ConfigError, match="warning > critical"):
        parse_metrics_config(_config("""  memory.available:
    surfaces: { inspection: true }
    warning: 100
    critical: 200
    severity_enabled: true
"""))


def test_enabled_metric_requires_at_least_one_surface():
    with pytest.raises(ConfigError, match="至少启用一个 surface"):
        parse_metrics_config(_config("""  cpu.usage:
    enabled: true
    surfaces: { inspection: false, chat: false, alert_analysis: false }
"""))


def test_alert_surfaces_require_range_support():
    with pytest.raises(ConfigError, match="不支持 alert_analysis"):
        parse_metrics_config(_config("""  alerts.active_count:
    surfaces: { inspection: true, chat: false, alert_analysis: true }
"""))


def test_explicit_missing_file_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="指标配置文件不存在"):
        load_metrics_config(tmp_path / "missing.yaml")
