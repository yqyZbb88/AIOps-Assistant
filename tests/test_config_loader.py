"""config_loader：${VAR} / ${VAR:-default} 插值与 dataclass 解析。"""
from __future__ import annotations

from src.utils.config_loader import (
    Settings,
    interpolate,
    load_settings,
)


def test_interpolate_with_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_TEST_VAR", raising=False)
    # 有默认值：变量缺失回退默认
    assert interpolate("http://${MY_TEST_VAR:-localhost:9090}/x") == "http://localhost:9090/x"
    # 变量存在则用环境值
    monkeypatch.setenv("MY_TEST_VAR", "prom:1")
    assert interpolate("http://${MY_TEST_VAR:-localhost:9090}/x") == "http://prom:1/x"


def test_interpolate_required_missing_is_empty(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_REQUIRED_VAR", raising=False)
    # 无默认值的变量缺失 → 空字符串（保证启动/加载不失败）
    assert interpolate("pre${MY_REQUIRED_VAR}post") == "prepost"


def test_load_settings_parses_types_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROM_URL_TEST", "http://my-prom:9999")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(
        "\n".join(
            [
                "app:",
                "  name: ops-test",
                "  version: 9.9.9",
                "server:",
                "  host: 0.0.0.0",
                "  port: 9999",
                "prometheus:",
                "  base_url: ${PROM_URL_TEST:-http://localhost:9090}",
                "  timeout: 8",
                "llm:",
                "  enabled: false",
                "logging:",
                "  level: DEBUG",
                "",
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings(cfg)
    assert settings.app.name == "ops-test"
    assert settings.server.port == 9999
    assert settings.prometheus.base_url == "http://my-prom:9999"
    assert settings.prometheus.timeout == 8
    assert settings.llm.enabled is False
    assert settings.logging.level == "DEBUG"
    # 未写的节回退默认
    assert settings.monitor.provider == "prometheus"
    assert settings.alert.recent_limit == 100


def test_load_settings_missing_file_returns_defaults(tmp_path, monkeypatch):
    settings = load_settings(tmp_path / "nope.yaml")
    assert isinstance(settings, Settings)
    assert settings.app.name  # 非空默认


def test_default_settings():
    s = Settings()
    assert s.server.port == 8000
    assert s.alert.trend_step == "5m"
