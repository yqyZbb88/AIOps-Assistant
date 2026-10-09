"""模型入口：按配置版本复用 init.py 惰性工厂创建的实例。

- 顶层不导入 langchain 或 init，真正取模型时才惰性导入；
- 同一配置版本的聊天 Agent 和 LLM 客户端共享实例；旧请求保留原实例；
- 模型初始化失败时抛出 ModelLoadError，由调用方转换为对应接口错误。
"""
from __future__ import annotations

import sys
import threading
import hashlib
import json
from typing import Any

from src.utils.config_loader import ROOT_DIR


class ModelLoadError(Exception):
    """模型初始化失败（可选依赖缺失 / 模型凭据未配 / init.py 报错）。"""


# 进程内缓存的模型对象（首次调用时惰性创建；全进程共用）
_model: Any = None
_models: dict[str, Any] = {}
_model_lock = threading.RLock()


def get_model(config: dict | None = None) -> Any:
    """惰性返回 init.py 中创建的模型对象（首次调用时才 import init.py）。

    凭据仅来自显式配置或容器环境，不加载宿主机 .env。
    """
    global _model
    if config is not None:
        fingerprint = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        with _model_lock:
            if fingerprint not in _models:
                try:
                    if str(ROOT_DIR) not in sys.path:
                        sys.path.insert(0, str(ROOT_DIR))
                    from init import build_model
                    _models[fingerprint] = build_model(dict(config))
                    if len(_models) > 16:
                        del _models[next(iter(_models))]
                except Exception:
                    raise ModelLoadError("LLM 初始化失败，请检查模型配置或依赖") from None
            return _models[fingerprint]
    with _model_lock:
        if _model is None:
            try:
                if str(ROOT_DIR) not in sys.path:
                    sys.path.insert(0, str(ROOT_DIR))
                from init import model  # 复用 init.py：单一模型入口

                _model = model
            except Exception:
                raise ModelLoadError("LLM 初始化失败，请检查模型配置或依赖") from None
        return _model


def content_text(content: str | list[dict[str, str]]) -> str:
    """把消息 content（str 或消息块列表）归一化为纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    return str(content)
