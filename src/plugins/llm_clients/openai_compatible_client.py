"""OpenAI 兼容接口文本客户端；与聊天 Agent 共用模型入口和版本缓存。"""
from __future__ import annotations
from src.interfaces.llm_client import LLMClient, LLMError
from src.utils.model_loader import ModelLoadError, content_text, get_model


class OpenAICompatibleLLMClient(LLMClient):
    name = "openai_compatible"
    description = "OpenAI 兼容接口文本补全"

    def __init__(self, *, model_config=None):
        self.model_config = model_config

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        if not (prompt or "").strip():
            raise LLMError("prompt 为空")
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            messages = ([SystemMessage(content=system)] if system else []) + [HumanMessage(content=prompt)]
            response = get_model(self.model_config).invoke(messages)
            text = content_text(response.content).strip()
            if not text:
                raise LLMError("LLM 返回为空")
            return text
        except (LLMError, ModelLoadError) as exc:
            raise LLMError(str(exc)) from None
        except Exception:
            raise LLMError("模型请求失败，请检查模型服务、认证或超时设置") from None
