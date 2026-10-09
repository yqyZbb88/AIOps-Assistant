"""DeepSeek LLM 客户端插件，实现无工具场景下的文本补全。

惰性复用根目录 init.py 创建的模型实例；未安装可选依赖时模块仍可安全导入，
真正调用 complete() 时才抛出 LLMError。
"""
from __future__ import annotations

from src.interfaces.llm_client import LLMClient, LLMError
from src.utils.model_loader import ModelLoadError, content_text, get_model


class DeepseekLLMClient(LLMClient):
    """DeepSeek 直连补全客户端。"""

    name = "deepseek"
    description = "DeepSeek 文本补全（复用根目录 init.py 的 init_chat_model 模型）"

    def __init__(self, *, model_config=None):
        self.model_config = model_config

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

        try:
            model = get_model(self.model_config) if self.model_config is not None else get_model()
        except ModelLoadError as exc:
            raise LLMError(str(exc)) from exc

        try:
            resp = model.invoke(messages)
        except Exception as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc

        text = content_text(resp.content).strip()
        if not text:
            raise LLMError("LLM 返回为空")
        return text
