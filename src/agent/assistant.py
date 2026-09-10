"""智能运维 Agent 封装（对齐「尚硅谷-07-智能体」第 9 节推荐格式，重构后保留编排层）。

讲义第 9 节《实战：多功能智能助手》结构：模型初始化 → @tool 工具 →
create_agent(model, tools, system_prompt) → 封装类(chat/reset) → invoke 取末条 AI 消息。
本模块把这段结构落到运维场景，成为聊天链路的主入口：

    模型     = 惰性复用根目录 init.py 的 init_chat_model（经 utils/model_loader 取用，单一入口）
    工具     = agent/tools.py 的 build_tools(monitor)（CPU/内存/磁盘/告警，注入 monitor）
    封装类   = OpsAssistant（reply=单轮对话；chat/reset 预留多轮）

与旧 ops_bot/assistant.py 的差异：
- OpsAssistant(monitor=…)：工具绑定注入的 MonitorSource，不再 import 全局 prometheus 模块；
- analyze_alert / ask_llm 已迁至 core/analysis.py + plugins/llm_clients（走 LLMClient 接口）；
- 不再维护模块级全局单例：由 container 装配，经工厂注入 ChatService。

预留扩展接口（用户要求方便后面扩展）：
    - name / debug            : create_agent 的归因与调试开关（可观测）；
    - response_format         : 结构化输出（讲义 7.3 ToolStrategy），默认 None 保持自然语言；
    - interrupt_before        : 未来把"动作执行工具"接入时做人工审批（见 tools.py 预留注释）；
    - memory / checkpointer   : 需要跨请求多轮对话时再加（当前 reply 为单轮，无状态）。

模块顶层不 import langchain：只有构建 OpsAssistant（_build_agent）时才惰性导入；
因此未装可选依赖的环境 import 本模块不报错，构建失败由上层（ChatService）回落关键词。
"""
from __future__ import annotations

import logging
from typing import Any

from src.interfaces.diagnostic import DiagnosticSource
from src.interfaces.monitor import MonitorSource
from src.utils.model_loader import ModelLoadError, content_text, get_model

logger = logging.getLogger("ops_agent.agent")


class LLMError(Exception):
    """LLM 初始化 / 调用失败时抛出。"""


# ---------------------------------------------------------------------------
# Agent 构建与封装类（讲义第 9.3 节结构）
# ---------------------------------------------------------------------------
def _build_agent(
    *,
    model,
    tools: list,
    system_prompt: str,
    name: str,
    response_format=None,
    debug: bool = False,
):
    """调用 create_agent 构建一个 LangGraph 智能体（惰性 import langchain）。

    - name 用 AGENT_NAME（日志/trace 归因，讲义第 5 节）；
    - debug 传 create_agent 的 debug 开关（可观测，默认关）；
    - response_format 为结构化输出预留参数（默认 None = 自然语言）。
    """
    from langchain.agents import create_agent

    agent = create_agent(
        model=model,
        tools=list(tools),
        system_prompt=system_prompt,
        name=name,
        debug=debug,
        response_format=response_format,
    )
    logger.debug("Agent 构建完成 name=%s tools=%d", name, len(tools))
    return agent


def _last_ai_content(response: dict[str, Any]) -> str:
    """从 agent.invoke 返回的 response 中取最后一条有内容的 AI 消息文本。"""
    messages = response.get("messages", [])
    for msg in reversed(messages):
        # msg.type == "ai" 且 content 非空才算最终回答（工具调用轮 content 常为空）
        if getattr(msg, "type", "") == "ai" and msg.content:
            return content_text(msg.content)
    return "抱歉，我暂时无法回答这个问题。"


class OpsAssistant:
    """智能运维 Agent 封装类（对齐讲义第 9.3 节 SmartAssistant 结构）。

    字段/方法均与讲义保持一致并预留扩展：
        model / tools / system_prompt / agent / messages / chat / reset
    区别：HTTP 服务无状态，reply() 每次传入完整上下文做单轮调用，不跨请求累积历史；
    chat()/reset() 保留讲义原样的多轮接口，供需要记忆时扩展。
    """

    def __init__(
        self,
        monitor: MonitorSource,
        *,
        diag: DiagnosticSource | None = None,
        name: str = "ops_assistant",
        system_prompt: str | None = None,
        response_format=None,
        debug: bool = False,
    ) -> None:
        # 本构造会触发 langchain / init.py 惰性导入；失败向上抛，由调用方回落
        from src.agent.prompts import AGENT_NAME, OPS_SYSTEM_PROMPT
        from src.agent.tools import build_diag_tools, build_tools

        if monitor is None:
            raise ValueError("OpsAssistant 需要注入 monitor（MonitorSource 实现）")
        self.monitor = monitor
        self.diag = diag
        self.name = name or AGENT_NAME
        self.system_prompt = system_prompt or OPS_SYSTEM_PROMPT
        self.debug = debug

        # 监控工具 + 只读诊断工具（诊断源为 None 时不注册，Agent 看不到该能力）
        self.tools = build_tools(monitor)
        if diag is not None:
            self.tools = [*self.tools, *build_diag_tools(diag)]
        # 惰性复用 init.py 模型入口（utils/model_loader 单例缓存）；模型/provider 只改 init.py
        try:
            self.model = get_model()
        except ModelLoadError as exc:
            raise LLMError(str(exc)) from exc
        self.agent = _build_agent(
            model=self.model,
            tools=self.tools,
            system_prompt=self.system_prompt,
            name=self.name,
            response_format=response_format,
            debug=debug,
        )
        # 对话历史（预留：chat()/reset() 用；reply() 单轮不依赖它）
        self.messages: list = []

    # -- 单轮对话（HTTP 主入口） -------------------------------------------
    def reply(self, message: str) -> str:
        """单轮对话：把用户消息交给 Agent，返回最终回答文本。

        模型可自主决定是否调用监控工具（cpu_usage 等）后组织回答。
        """
        if not (message or "").strip():
            raise LLMError("消息为空")
        logger.info("Agent 收到消息: %s", message)
        response = self.agent.invoke({"messages": [{"role": "user", "content": message}]})
        return _last_ai_content(response)

    # -- 讲义原样接口（预留多轮扩展） --------------------------------------
    def chat(self, user_input: str) -> str:
        """讲义第 9.3 节对话接口：维护 self.messages 历史做多轮对话。"""
        self.messages.append({"role": "user", "content": user_input})
        response = self.agent.invoke({"messages": self.messages})
        self.messages = response["messages"]  # 类型: list[BaseMessage]
        return _last_ai_content(response)

    def reset(self) -> None:
        """重置对话历史。"""
        self.messages = []
