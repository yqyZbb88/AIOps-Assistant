"""通用模型入口：解析配置、惰性构建并按配置版本共享实例。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from typing import Any
from urllib.parse import urlsplit

_MODEL_ENV = {
    "api_key": ("MODEL_API_KEY", ""), "base_url": ("MODEL_BASE_URL", ""),
    "model": ("MODEL_NAME", ""), "timeout": ("MODEL_TIMEOUT", "30"),
    "max_retries": ("MODEL_MAX_RETRIES", "2"), "max_tokens": ("MODEL_MAX_TOKENS", "2048"),
    "token_limit_param": ("MODEL_TOKEN_LIMIT_PARAM", "max_tokens"),
}
_models: dict[str, Any] = {}
_model_lock = threading.RLock()


class ModelLoadError(Exception):
    """模型配置或构建失败；公开错误不包含供应商返回内容及密钥。"""


def model_config(config: dict | None = None, *, require_complete: bool = False) -> dict:
    """页面配置整组优先；显式传入时不从环境补密钥或其他字段。"""
    values = {key: default for key, (_, default) in _MODEL_ENV.items()}
    if config is None:
        from src.utils.config_migration import reject_legacy_environment
        reject_legacy_environment(os.environ)
        values.update({key: os.getenv(env, default) for key, (env, default) in _MODEL_ENV.items()})
    else:
        if not isinstance(config, dict) or set(config) - set(values):
            raise ValueError("模型配置包含未知字段")
        values.update(config)
    for key in ("api_key", "base_url", "model", "token_limit_param"):
        if not isinstance(values[key], str):
            raise ValueError("模型文本参数格式无效")
    if len(values["api_key"]) > 8192 or len(values["model"]) > 128 or len(values["base_url"]) > 2048:
        raise ValueError("模型文本参数过长")
    values["model"] = values["model"].strip()
    values["base_url"] = values["base_url"].strip().rstrip("/")
    if values["base_url"]:
        url = urlsplit(values["base_url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("模型 Base URL 必须是无凭据的 HTTP(S) 接口根地址")
    if values["token_limit_param"] not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError("输出长度参数须为 max_tokens 或 max_completion_tokens")
    try:
        if any(isinstance(values[key], bool) for key in ("timeout", "max_retries", "max_tokens")):
            raise ValueError
        timeout = float(values["timeout"])
        retries, tokens = int(values["max_retries"]), int(values["max_tokens"])
        if str(values["max_retries"]) not in {str(retries), str(float(retries))} or str(values["max_tokens"]) not in {str(tokens), str(float(tokens))}:
            raise ValueError
        if not math.isfinite(timeout) or not 0 < timeout <= 300 or not 0 <= retries <= 10 or not 1 <= tokens <= 131072:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ValueError("模型参数无效：超时 0~300 秒、重试 0~10、输出 1~131072") from None
    values.update(timeout=timeout, max_retries=retries, max_tokens=tokens)
    if require_complete and not all(values[key] for key in ("api_key", "base_url", "model")):
        raise ValueError("请配置 MODEL_API_KEY、MODEL_BASE_URL 和 MODEL_NAME，或在管理页保存完整模型配置")
    return values


def build_model(config: dict | None = None) -> Any:
    """构造不发网络请求；兼容接口只使用标准聊天、工具调用和输出长度参数。"""
    values = model_config(config, require_complete=True)
    from langchain_openai import ChatOpenAI
    # 输出长度放入 SDK 的请求体扩展，防止 ChatOpenAI 自动重命名 max_tokens。
    # 不传 temperature、推理或 Responses 专属参数，减少兼容服务的协议差异。
    return ChatOpenAI(
        model=values["model"], api_key=values["api_key"], base_url=values["base_url"],
        timeout=values["timeout"], max_retries=values["max_retries"],
        use_responses_api=False, stream_usage=False,
        extra_body={values["token_limit_param"]: values["max_tokens"]},
    )


def get_model(config: dict | None = None) -> Any:
    """同一有效配置复用实例；切换配置不修改旧请求持有的实例。"""
    try:
        values = model_config(config, require_complete=True)
        from src.utils.security import register_secrets
        register_secrets(values["api_key"])
        fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        with _model_lock:
            if fingerprint not in _models:
                _models[fingerprint] = build_model(values)
                if len(_models) > 16:
                    del _models[next(iter(_models))]
            return _models[fingerprint]
    except Exception:
        raise ModelLoadError("LLM 初始化失败，请检查完整模型配置或依赖；旧配置请先运行 migrate-config") from None


def content_text(content: Any) -> str:
    """把文本及消息块归一化，不返回工具参数或推理元数据。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text") or block.get("content") or "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)
