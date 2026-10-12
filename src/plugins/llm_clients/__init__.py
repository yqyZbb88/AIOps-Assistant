"""注册通用模型客户端；模块导入不构建模型。"""
from src.plugins.llm_clients.openai_compatible_client import OpenAICompatibleLLMClient
from src.registry import registry

registry.register_llm(OpenAICompatibleLLMClient.name, OpenAICompatibleLLMClient)
