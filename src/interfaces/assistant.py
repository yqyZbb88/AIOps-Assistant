"""Assistant：聊天 Agent 契约（结构化 Protocol）。

core/chat.py 的 ChatService 只需「给一条消息、拿回一段文本」，
不感知 langchain / langgraph，也不感知模型如何构建——这正是本契约存在的意义：
- 生产实现见 src/agent/assistant.py::OpsAssistant（惰性构建 langchain Agent）；
- 测试可注入任意同形状假对象（见 tests/test_chat.py 的 FakeAssistant），无需装 langchain。

用 Protocol 而非 ABC：实现方（agent 层）不必知道自己实现了本接口，
也避免 core → agent 的反向 import。typing.Protocol 是标准库。
"""
from __future__ import annotations

from typing import Protocol


class Assistant(Protocol):
    """聊天 Agent：单轮对话，返回最终回答文本。"""

    def reply(self, message: str) -> str:
        """把用户消息交给 Agent，返回回答文本；失败时抛异常由调用方回落。"""
        ...
