"""DeepSeek 唯一模型构建入口；配置校验和依赖导入均在调用工厂时进行。"""
import math
import os


def build_model(config=None):
    values = {"api_key": os.getenv("DEEPSEEK_API_KEY"), "base_url": os.getenv("DEEPSEEK_BASE_URL"),
              "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
              "timeout": os.getenv("DEEPSEEK_TIMEOUT", "30"),
              "max_retries": os.getenv("DEEPSEEK_MAX_RETRIES", "2"),
              "max_tokens": os.getenv("DEEPSEEK_MAX_TOKENS", "2048")}
    if config is not None:
        values.update(config)
    try:
        timeout = float(values["timeout"])
        retries, tokens = int(values["max_retries"]), int(values["max_tokens"])
    except (ValueError, TypeError):
        raise ValueError("DEEPSEEK_TIMEOUT / DEEPSEEK_MAX_RETRIES / DEEPSEEK_MAX_TOKENS 格式无效") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("DEEPSEEK_TIMEOUT 必须为有限正数")
    if retries < 0:
        raise ValueError("DEEPSEEK_MAX_RETRIES 必须为非负整数")
    if tokens < 1:
        raise ValueError("DEEPSEEK_MAX_TOKENS 必须为正整数")
    from langchain.chat_models import init_chat_model
    return init_chat_model(model=values["model"], model_provider="deepseek", api_key=values["api_key"],
                           base_url=values["base_url"] or None, timeout=timeout,
                           max_retries=retries, max_tokens=tokens)


_legacy_model = None


def __getattr__(name):
    """兼容 from init import model；模块导入本身不构建模型。"""
    if name != "model":
        raise AttributeError(name)
    global _legacy_model
    if _legacy_model is None:
        _legacy_model = build_model()
    return _legacy_model
