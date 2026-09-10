"""interfaces：抽象接口层。

core / api 只依赖这里的抽象基类与异常，不直接依赖具体插件实现；
具体实现位于 src/plugins/，由 src/container.py 装配注入（可替换、可测试）。
"""

from src.interfaces.action import ActionError, ActionExecutor
from src.interfaces.llm_client import LLMClient, LLMError
from src.interfaces.monitor import MonitorError, MonitorSource

__all__ = [
    "MonitorSource",
    "MonitorError",
    "ActionExecutor",
    "ActionError",
    "LLMClient",
    "LLMError",
]
