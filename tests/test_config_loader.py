"""config_loader：${VAR} / ${VAR:-default} 插值与 dataclass 解析。"""
from __future__ import annotations

import pytest

from src import __app_name__, __version__
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
    # 缺失变量在插值阶段变为空串；后续字段校验仍可能拒绝该值。
    assert interpolate("pre${MY_REQUIRED_VAR}post") == "prepost"


def test_load_settings_parses_types_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PROM_URL_TEST", "http://my-prom:9999")
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(
        "\n".join(
            [
                "app:",
                "  name: ops-test",
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


def test_runtime_controls_are_parsed(tmp_path):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text("""auth:
  enabled: false
agent:
  enabled: false
  recursion_limit: 7
chat:
  max_message_chars: 123
metrics:
  file: config/custom_metrics.yaml
""", encoding="utf-8")

    settings = load_settings(cfg)

    assert settings.agent.enabled is False
    assert settings.agent.recursion_limit == 7
    assert settings.chat.max_message_chars == 123
    assert settings.metrics.file == "config/custom_metrics.yaml"


def test_app_name_and_version_have_single_source():
    settings = Settings()
    assert settings.app.name == __app_name__ == "AIOps-Assistant"
    assert settings.app.version == __version__ == "0.6.1"


def test_settings_dataclass_defaults():
    """裸构造 `Settings()` 就得可用（测试里 create_app(settings=Settings()) 是常态）。

    注意这是 **dataclass 字段默认值**，不是「读不到配置文件时的兜底」——
    `load_settings` 没有兜底，文件缺失一律 ConfigError（见其模块注释）。
    """
    s = Settings()
    assert s.server.port == 8000
    assert s.alert.trend_step == "5m"


def test_auth_is_disabled_by_default():
    """裸 Settings 默认关鉴权；文件启动使用其配置，新 Compose 模板默认开启。"""
    s = Settings()
    assert s.auth.enabled is False
    assert s.auth.tokens == ""


def test_auth_section_is_parsed_from_yaml(tmp_path, monkeypatch):
    """带引号的 JSON 占位保持字符串类型；未加引号的映射不符合 tokens 类型。"""
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



def test_yaml_interpolation_preserves_quoted_values_and_unquoted_types(monkeypatch):
    import yaml
    from src.utils.config_loader import interpolate_yaml
    values = ['test"quote', "test'apostrophe", "test\\path", "test\nline", "Ｔｅｓｔ①", "  test  "]
    for value in values:
        monkeypatch.setenv("TEST_QUOTED_VALUE", value)
        for quote in ["'", '"']:
            text = "value: " + quote + "prefix${TEST_QUOTED_VALUE}suffix" + quote + "\n"
            assert yaml.safe_load(interpolate_yaml(text))["value"] == "prefix" + value + "suffix"
    monkeypatch.setenv("TEST_NUMBER", "22")
    monkeypatch.setenv("TEST_BOOL", "false")
    parsed = yaml.safe_load(interpolate_yaml("port: ${TEST_NUMBER}\nenabled: ${TEST_BOOL}\n"))
    assert parsed == {"port": 22, "enabled": False}


def test_quoted_auth_json_accepts_apostrophe_in_token(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("TEST_TOKEN_JSON", json.dumps({"test'token": "test-user"}))
    path = tmp_path / "settings.yaml"
    path.write_text("auth:\n  enabled: true\n  tokens: '${TEST_TOKEN_JSON}'\n", encoding="utf-8")
    settings = load_settings(path)
    assert json.loads(settings.auth.tokens) == {"test'token": "test-user"}


@pytest.mark.parametrize("kind", ["settings", "hosts", "metrics", "actions", "agent_actions", "diagnostics", "rules"])
def test_yaml_syntax_error_does_not_expose_interpolated_credentials(tmp_path, monkeypatch, kind):
    import traceback
    from src.utils.hosts_config import parse_hosts
    from src.utils.metrics_config import parse_metrics_config
    from src.plugins.actions.catalog import parse_actions
    from src.plugins.agent_actions.catalog import parse_agent_actions
    from src.plugins.diagnostics.commands import parse_diagnostics
    from src.core.rule_engine import RuleEngine

    monkeypatch.setenv("TEST_YAML_SECRET", 'fake"key\\z')
    text = "auth:\n  enabled: true\n  tokens: '${TEST_YAML_SECRET}' trailing\n"
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    parsers = {
        "settings": lambda: load_settings(path),
        "hosts": lambda: parse_hosts(text),
        "metrics": lambda: parse_metrics_config(text),
        "actions": lambda: parse_actions(text),
        "agent_actions": lambda: parse_agent_actions(text),
        "diagnostics": lambda: parse_diagnostics(text),
        "rules": lambda: RuleEngine(rules_text=text),
    }
    with pytest.raises(Exception) as caught:
        parsers[kind]()
    diagnostic = "".join(traceback.format_exception(caught.value))
    assert "fake" not in diagnostic
    assert "第 3 行" in diagnostic


@pytest.mark.parametrize("text", [
    "auth:\n  tokens: ${TEST_TOKEN_MAPPING}\n",
    "auth: [${TEST_TOKEN_MAPPING}]\n",
    "auth:\n  enabled: false\nserver:\n  port: '${TEST_TOKEN_MAPPING}'\n",
])
def test_settings_type_error_does_not_echo_credentials(tmp_path, monkeypatch, text):
    import json
    import traceback

    monkeypatch.setenv("TEST_TOKEN_MAPPING", json.dumps({"fake-credential": "admin"}))
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_settings(path)
    assert "fake-credential" not in "".join(traceback.format_exception(caught.value))
    assert "auth" in str(caught.value) or "server.port" in str(caught.value)
