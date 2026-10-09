"""模型环境参数验证：替换 dotenv 和模型工厂，全程离线且不读取密钥。"""
from pathlib import Path
import runpy
import sys
from types import ModuleType

import pytest


def _load(monkeypatch, overrides):
    calls = []
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = lambda **kwargs: False
    chat_models = ModuleType("langchain.chat_models")
    chat_models.init_chat_model = lambda **kwargs: calls.append(kwargs) or object()
    monkeypatch.setitem(sys.modules, "dotenv", dotenv)
    monkeypatch.setitem(sys.modules, "langchain.chat_models", chat_models)
    defaults = {"DEEPSEEK_API_KEY": "synthetic-model-key", "DEEPSEEK_BASE_URL": "",
                "DEEPSEEK_MODEL": "test-model", "DEEPSEEK_TIMEOUT": "30",
                "DEEPSEEK_MAX_RETRIES": "2", "DEEPSEEK_MAX_TOKENS": "2048"}
    for key, value in {**defaults, **overrides}.items():
        monkeypatch.setenv(key, value)
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "init.py"))
    assert calls == [], "导入模型入口不应构建客户端"
    namespace["model"] = namespace["build_model"]()
    return namespace, calls


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_invalid_model_timeout_is_rejected_before_client_creation(monkeypatch, value):
    with pytest.raises(ValueError, match="DEEPSEEK_TIMEOUT"):
        _load(monkeypatch, {"DEEPSEEK_TIMEOUT": value})


@pytest.mark.parametrize("key,value", [("DEEPSEEK_MAX_RETRIES", "-1"), ("DEEPSEEK_MAX_TOKENS", "0"), ("DEEPSEEK_MAX_TOKENS", "-1")])
def test_invalid_model_integer_bounds_are_rejected(monkeypatch, key, value):
    with pytest.raises(ValueError, match=key):
        _load(monkeypatch, {key: value})


def test_valid_model_settings_keep_existing_defaults_and_allow_zero_retries(monkeypatch):
    namespace, calls = _load(monkeypatch, {"DEEPSEEK_MAX_RETRIES": "0", "DEEPSEEK_TIMEOUT": "0.5"})
    assert len(calls) == 1
    assert calls[0]["timeout"] == 0.5
    assert calls[0]["max_retries"] == 0
    assert calls[0]["max_tokens"] == 2048
    assert namespace["model"] is not None
