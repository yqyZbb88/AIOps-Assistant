"""聊天链路 ChatService：Agent-first + 关键词兜底。

承接原 ops_bot/bot.py 的 get_reply 与关键词规则：
    主链路  用户消息 → Agent（DeepSeek + 监控工具，见 src/agent/assistant.py）；
            模型自主判断是否查 CPU/内存/磁盘/告警。
    兜底    Agent 构建/调用不可用（未装 langchain、.env 缺 key、网络失败）→
            本模块关键词规则：CPU/内存/磁盘/告警/状态/帮助，仍可答复监控指标。
    未命中 → DEFAULT_REPLY。

重构要点：
- 关键词 handler 转发 src/core/capabilities.py 的纯函数（与 Agent 工具同一套能力实现），
  绑定「注入的 MonitorSource」，不再 import 具体监控模块（可注入 FakeMonitor）；
- Agent 经 `assistant_factory` 注入（container 提供），ChatService 不感知 langchain：
  首次聊天时尝试构建，失败即回落并标记，服务在无 key / 无 langchain 环境照常运行。

扩展方式：
    - 新增监控能力：先在 src/core/capabilities.py 加一个 `xxx_text(monitor)` 纯函数，
      再到本模块 RULES 与 agent/tools.py build_tools 各转发一行（两处产出同款文案）；
    - 关键词兜底新增"状态/帮助"类静态规则：向 __init__ 里 RULES 追加 Rule(keyword, reply=...)。
"""
from __future__ import annotations

import logging
from typing import Callable

from src.core import capabilities
from src.interfaces.assistant import Assistant
from src.interfaces.monitor import MonitorSource

logger = logging.getLogger("ops_agent.chat")

# 帮助信息：列出现阶段支持的能力
HELP_TEXT = (
    "可用能力：\n"
    "  - AI 对话（默认）：直接问任意运维问题，我会按需查询监控数据回答\n"
    "  - 监控指标：CPU / 内存 / 磁盘（AI 会自动查询 Node Exporter 数据）\n"
    "  - 告警：查询当前 pending / firing 状态的告警\n"
    "  - 帮助：显示本帮助信息\n"
    "\n（提示：LLM 不可用时会回落到关键词模式，仅支持固定命令）\n"
    "\n示例：\n"
    '  {"user": "ops", "message": "帮我看看现在系统负载高不高？"}'
)

# 兜底回复：LLM 不可用时的最终兜底
DEFAULT_REPLY = "我不理解，请尝试其他问题"


class Rule:
    """一条关键词规则：消息命中 keyword 时，返回回复。

    reply：静态字符串；handler：无参可调用对象（运行时动态计算回复，如查询指标）。
    """

    def __init__(
        self,
        keyword: str | tuple[str, ...],
        reply: str | None = None,
        handler: Callable[[], str] | None = None,
        description: str = "",
    ) -> None:
        self.keywords: tuple[str, ...] = (
            (keyword,) if isinstance(keyword, str) else tuple(keyword)
        )
        self.reply = reply
        self.handler = handler
        self.description = description or f"命中关键词：{'/'.join(self.keywords)}"

    def match(self, message: str) -> bool:
        """子串匹配：任一触发词出现在消息中即命中。"""
        return any(keyword in message for keyword in self.keywords)

    def respond(self) -> str:
        """计算该规则回复：优先动态 handler，否则返回静态 reply。"""
        if self.handler is not None:
            return self.handler()
        return self.reply or ""


class ChatService:
    """聊天主服务：Agent-first → 关键词兜底 → 默认回复。"""

    def __init__(
        self,
        monitor: MonitorSource,
        *,
        assistant_factory: Callable[[], Assistant] | None = None,
    ) -> None:
        self.monitor = monitor
        # 注入的 Agent 工厂：返回 Assistant 契约对象；失败抛异常即回落
        self._assistant_factory = assistant_factory
        self._assistant: Assistant | None = None
        self._agent_broken = assistant_factory is None
        # 规则按优先级从高到低排列，第一条命中生效；动态 handler 与 Agent 工具转发
        # 同一批 capabilities 纯函数（src/core/capabilities.py），无 LLM 时产出同款规范文案
        self._rules = [
            Rule(
                "CPU",
                handler=lambda: capabilities.cpu_usage_text(self.monitor),
                description="查询 CPU 使用率",
            ),
            Rule(
                "内存",
                handler=lambda: capabilities.memory_usage_text(self.monitor),
                description="查询内存使用率",
            ),
            Rule(
                "磁盘",
                handler=lambda: capabilities.disk_usage_text(self.monitor),
                description="查询磁盘使用率",
            ),
            # 告警状态：主动查询监控源（/api/v1/alerts），支持中文/英文触发词
            Rule(
                ("告警", "pending", "firing"),
                handler=lambda: capabilities.active_alerts_text(self.monitor),
                description="查询当前 pending/firing 告警",
            ),
            Rule("状态", reply="系统运行正常（模拟）", description="查询系统运行状态"),
            Rule("帮助", reply=HELP_TEXT, description="显示帮助信息"),
        ]

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def reply(self, message: str) -> str:
        """根据输入消息返回回复（Agent-first，失败自动回落）。"""
        assistant = self._ensure_assistant()
        if assistant is not None:
            try:
                return assistant.reply(message)
            except Exception as exc:  # Agent 调用失败（网络等）→ 本轮回落关键词
                logger.warning("Agent 回复异常，回落关键词引擎: %s", exc)

        for rule in self._rules:
            if rule.match(message):
                return rule.respond()
        return DEFAULT_REPLY

    def _ensure_assistant(self) -> Assistant | None:
        """首次聊天时经工厂构建 Agent 并缓存；构建失败标记 broken 后回落。"""
        if self._assistant is not None:
            return self._assistant
        if self._agent_broken or self._assistant_factory is None:
            return None
        try:
            self._assistant = self._assistant_factory()
        except Exception as exc:  # 未装 langchain / .env 缺 key / init 失败等
            self._agent_broken = True
            logger.warning("Agent 构建不可用，回落关键词引擎: %s", exc)
        return self._assistant
