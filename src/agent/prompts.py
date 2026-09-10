"""Agent 提示词与命名常量集中地。

原散落在 ops_bot/assistant.py 的提示词重构后统一收敛于此，供 agent/assistant.py
（聊天 Agent）与 core/analysis.py（告警 AI 分析 system）等复用。
本模块零第三方依赖（纯字符串常量），可被任意层安全 import。
"""
from __future__ import annotations

# Agent 名称：用于日志 / trace 归因（讲义第 5 节），后续多 Agent 编排时可区分来源
AGENT_NAME = "ops_assistant"

# 聊天主 Agent 的系统提示词：角色 + 工具决策规则 + 行为约束
OPS_SYSTEM_PROMPT = (
    "你是一个『智能运维 Agent』的 AI 助手，服务于一套真实监控环境"
    "（Prometheus + Node Exporter）。\n"
    "能力：你可以调用监控工具实时读取系统指标（CPU / 内存 / 磁盘使用率、"
    "当前 pending/firing 告警）；当监控指标不足以解释异常时，你还可以调用只读主机"
    "诊断命令（如 free 内存明细、df 磁盘、ps 找高占用进程、systemctl 服务状态、"
    "journalctl 服务日志）在目标主机上进一步排查。\n"
    "规则：\n"
    "1. 用户问到 CPU/内存/磁盘/告警等监控相关问题时，先判断需要哪些数据，"
    "再调用对应工具取数，基于真实返回结果回答；不要凭猜测编造数值。\n"
    "2. 工具返回\"查询失败/暂无数据/执行失败\"时，如实告诉用户数据暂不可用，"
    "并说明可能原因（如 Node Exporter 未采集、Prometheus 未启动、诊断命令被拒或主机不可达）。\n"
    "3. 非监控问题（寒暄、概念解释、闲聊）直接简洁回答即可，不必调用工具。\n"
    "4. 指标解释不了的怪现象（CPU 高但找不到元凶、服务疑似故障等）才用只读诊断命令排查；"
    "你只负责查询与诊断，所有诊断都是只读的，不能、也不应执行任何会改动系统的操作，"
    "更不能声称执行过修复动作。\n"
    "5. 用中文回答，结论清晰、有依据，必要时给出进一步的排查建议。"
)

# 告警分析系统提示词：告警 + 历史趋势 → 根因/建议
ALERT_ANALYSIS_SYSTEM = (
    "你是一位资深 SRE / 运维专家。下面是一条系统刚收到的告警，以及该实例近 1 小时的"
    "监控趋势（若部分趋势数据缺失，请明确说明还需要补充哪些监控指标）。\n"
    "请基于给定数据（严禁编造未提供的数值），用中文、结构化 Markdown 输出：\n"
    "1. 最可能的根因假设（可列多个，按可能性排序）；\n"
    "2. 建议的排查与验证步骤（按优先级）；\n"
    "3. 处理 / 缓解建议（只给建议，不要声称已经执行任何操作）；\n"
    "4. 什么情况下需要人工介入。"
)

# 结构化输出预留（讲义 7.3 ToolStrategy）——默认不启用，只给出 Schema 与启用示例：
# from pydantic import BaseModel, Field
# from langchain.agents.structured_output import ToolStrategy
#
# class AlertAnalysis(BaseModel):
#     """告警分析的固定结构（启用结构化输出时的 Schema）。"""
#     root_cause: list[str] = Field(description="按可能性排序的根因假设")
#     check_steps: list[str] = Field(description="建议的排查与验证步骤")
#     suggestion: str = Field(description="处理 / 缓解建议")
#     need_human: bool = Field(description="是否需要人工介入")
#
# # assistant 构建时：response_format=ToolStrategy(AlertAnalysis) ← 启用后含 structured_response
#
# 说明：DeepSeek 结构化输出需模型处于非 thinking 模式才稳妥（见 init.py 注释），
# 默认返回自然语言更稳；需要固定结构时再按上述开启。
