"""模型入口：进程内复用 init.py 创建的单一模型实例。

- 顶层不导入 langchain、dotenv 或 init，真正取模型时才惰性导入；
- 模型实例在进程内缓存，聊天 Agent 和 LLM 客户端共享同一份；
- 模型初始化失败时抛出 ModelLoadError，由调用方转换为对应接口错误。
"""
from __future__ import annotations

import sys
from typing import Any

from src.utils.config_loader import ROOT_DIR


class ModelLoadError(Exception):
    """模型初始化失败（可选依赖缺失 / .env 未配 / init.py 报错）。"""


# 进程内缓存的模型对象（首次调用时惰性创建；全进程共用）
_model: Any = None


def get_model() -> Any:
    """惰性返回 init.py 中创建的模型对象（首次调用时才 import init.py）。

    init.py 的 load_dotenv 依赖运行 cwd，这里显式加载仓库根 .env 兜底。
    """
    global _model
    if _model is None:
        try:
            try:
                from dotenv import load_dotenv

                load_dotenv(ROOT_DIR / ".env", override=False)
            except Exception:  # 未安装 python-dotenv 时忽略，交给 init.py 处理
                pass
            if str(ROOT_DIR) not in sys.path:
                sys.path.insert(0, str(ROOT_DIR))
            from init import model  # 复用 init.py：单一模型入口

            _model = model
        except Exception as exc:
            raise ModelLoadError(
                f"LLM 初始化失败（请检查 .env 的 DEEPSEEK_API_KEY 与 init.py）: {exc}"
            ) from exc
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
