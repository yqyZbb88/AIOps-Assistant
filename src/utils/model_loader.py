"""模型入口：惰性复用根 init.py 的**单一**模型实例。

聊天 Agent（src/agent/assistant.py）与 LLM 客户端（src/plugins/llm_clients/deepseek_client.py）
原先各自复制了一份「加载 init.py → 缓存模型」的逻辑，本模块是两处合并后的唯一实现。

设计：
- 顶层不 import langchain / dotenv / init：真正取模型时才惰性导入，
  未装可选依赖的环境 import 本模块不报错；
- 模型实例**进程内只有一份**（既定决策 #4「模型入口收敛」），两处调用方共享缓存；
- 不自行推算仓库根路径，复用 config_loader.ROOT_DIR（utils 内部依赖，叶子层语义不变）；
- 失败抛 ModelLoadError，由调用方映射为各自接口的 LLMError，保持 utils/ 零 src 内依赖。

注意：本模块**不要**加进 src/utils/__init__.py 的 import 链
（tests/test_no_langchain_import.py 会 `from src.utils import ...`，无需扩大该测试的 import 面）。
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
