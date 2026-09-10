"""DeepSeek LLM 客户端插件（实现 LLMClient.complete）。

承接原 ops_bot/llm.py 的 ask_llm「直连实现」：供告警 AI 分析等「无需工具」场景调用。
- 惰性复用根目录 init.py 的模型入口（init_chat_model），顶层不 import langchain / dotenv：
  未装可选依赖时本模块可被安全导入，真正 complete() 时才报 LLMError；
- 模型实例进程内缓存，重复调用不重复初始化。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from src.interfaces.llm_client import LLMClient, LLMError

# 项目根目录（init.py / .env 所在处）；config_loader 已按仓库根定位
_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent.parent

# 进程内缓存的模型对象（首次 complete 时惰性创建）
_model: Any = None


def _get_model() -> Any:
    """惰性返回 init.py 中创建的模型对象（复用单一模型入口）。"""
    global _model
    if _model is not None:
        return _model
    try:
        try:
            from dotenv import load_dotenv

            load_dotenv(_PROJECT_ROOT / ".env", override=True)
        except Exception:  # 未安装 python-dotenv 时忽略，交给 init.py
            pass
        if str(_PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(_PROJECT_ROOT))
        from init import model  # 复用 init.py：单一模型入口

        _model = model
    except Exception as exc:
        raise LLMError(
            f"LLM 初始化失败（请检查 .env 的 DEEPSEEK_API_KEY 与 init.py）: {exc}"
        ) from exc
    return _model


def _content_text(content: str | list[dict[str, str]]) -> str:
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


class DeepseekLLMClient(LLMClient):
    """DeepSeek 直连补全客户端。"""

    name = "deepseek"
    description = "DeepSeek 文本补全（复用根目录 init.py 的 init_chat_model 模型）"

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        if not (prompt or "").strip():
            raise LLMError("prompt 为空")

        content = prompt

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
        except ImportError as exc:
            raise LLMError(
                "缺少可选依赖 langchain-core，请先安装：pip install langchain-core"
            ) from exc

        messages = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=content))

        model = _get_model()
        try:
            resp = model.invoke(messages)
        except Exception as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc

        text = _content_text(resp.content).strip()
        if not text:
            raise LLMError("LLM 返回为空")
        return text
