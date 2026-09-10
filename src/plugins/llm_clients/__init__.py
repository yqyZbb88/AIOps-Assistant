"""LLM 客户端插件：导入即注册到 registry（键 = 供应商名）。"""
from src.plugins.llm_clients import deepseek_client  # noqa: F401
from src.registry import registry

registry.register_llm(
    deepseek_client.DeepseekLLMClient.name,
    deepseek_client.DeepseekLLMClient,
)
