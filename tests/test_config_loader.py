"""config_loader：${VAR} / ${VAR:-default} 插值与 dataclass 解析。"""
from __future__ import annotations

from src.utils.config_loader import (
    ConfigError,
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
                "auth:",
                "  enabled: false",
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


def test_load_settings_missing_file_is_rejected(tmp_path, monkeypatch):
    """配置缺失不能静默回退到 auth.enabled=false 的默认值。"""
    import pytest

    with pytest.raises(ConfigError, match="配置文件不存在"):
        load_settings(tmp_path / "nope.yaml")


def test_load_settings_requires_explicit_auth_section(tmp_path):
    import pytest

    cfg = tmp_path / "settings.yaml"
    cfg.write_text("""app:
  name: no-auth
""", encoding="utf-8")

    with pytest.raises(ConfigError, match="必须显式声明 auth"):
        load_settings(cfg)


def test_load_settings_rejects_unknown_fields(tmp_path):
    import pytest

    cfg = tmp_path / "settings.yaml"
    cfg.write_text("""auth:
  enabled: true
  enabledd: false
""", encoding="utf-8")

    with pytest.raises(ConfigError, match="未知字段: enabledd"):
        load_settings(cfg)


def test_load_settings_rejects_invalid_bool_instead_of_disabling(tmp_path):
    import pytest

    cfg = tmp_path / "settings.yaml"
    cfg.write_text("""auth:
  enabled: tru
""", encoding="utf-8")

    with pytest.raises(ConfigError, match="auth.enabled 必须是布尔值"):
        load_settings(cfg)


def test_load_settings_rejects_malformed_yaml(tmp_path):
    import pytest

    cfg = tmp_path / "settings.yaml"
    cfg.write_text("""auth: [
""", encoding="utf-8")

    with pytest.raises(ConfigError, match="配置文件解析失败"):
        load_settings(cfg)


def test_default_settings():
    s = Settings()
    assert s.server.port == 8000
    assert s.alert.trend_step == "5m"


def test_auth_is_disabled_by_default():
    """鉴权必须默认关：`create_app(settings=Settings())` 在测试里被调多次，
    默认打开会让既有用例集体 401。出厂是否打开只由 config/settings.yaml 决定
    （与 storage.enabled 同一条纪律）。"""
    s = Settings()
    assert s.auth.enabled is False
    assert s.auth.tokens == ""


def test_auth_section_is_parsed_from_yaml(tmp_path, monkeypatch):
    """yaml 里的占位符**必须用单引号包住**，否则变量有值时会被 YAML 解析成流式映射
    （dict），`_as_scalar` 再 `str()` 一下就成了单引号 repr，JSON 解析必炸。"""
    monkeypatch.setenv("OPS_API_TOKENS_TEST", '{"k-alice": "alice"}')
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(
        "\n".join(["auth:", "  enabled: true", "  tokens: '${OPS_API_TOKENS_TEST:-}'", ""]),
        encoding="utf-8",
    )

    settings = load_settings(cfg)

    assert settings.auth.enabled is True
    assert settings.auth.tokens == '{"k-alice": "alice"}'
    # 关键：拿到的是**字符串**而不是被 YAML 解出来的 dict
    assert isinstance(settings.auth.tokens, str)
