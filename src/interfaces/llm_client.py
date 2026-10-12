"""LLMClient：LLM 客户端抽象接口。

core/analysis 做告警 AI 分析时只调用本接口的 complete()，
不直接依赖具体模型供应商；实现见 src/plugins/llm_clients/。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class LLMError(Exception):
    """LLM 初始化 / 调用失败。"""


class LLMClient(ABC):
    """LLM 客户端：单次补全（prompt → 生成文本），供「无需工具」的场景直连。"""

    #: 供应商名（registry 键），如 "openai_compatible"
    name: str = ""

    @abstractmethod
    def complete(self, prompt: str, *, system: str | None = None) -> str:
        """把 prompt（可带 system 提示词）发给模型，返回生成文本。

        失败（未配置 / 网络 / 空返回）抛 LLMError。
        """
