"""运维工具集（对齐「尚硅谷-07-智能体」第 9.2 节工具定义格式）。

两块能力都封装成 LangChain 工具交给 Agent 按需调用：
1) `build_tools(monitor)`：读监控数据（CPU / 内存 / 磁盘 / 告警），闭包注入 MonitorSource；
2) `build_diag_tools(diag)`：只读主机诊断（free / df / ps / systemctl / journalctl），
   命令白名单在 config/diagnostics.yaml 声明，工具按白名单动态生成——Agent 只选命令名 +
   受控语义参数，凭据 / 自由命令永不进模型上下文。

设计约束（与旧 ops_bot/tools.py 一致）：
- 每个工具返回 str，docstring 写清 Args / Returns / Examples；
- 监控工具（build_tools）是**薄接线**：查询+格式化的真正实现收编在
  src/core/capabilities.py（无 langchain 纯函数，Agent 工具与关键词兜底共用同一套）；
  只读诊断工具（build_diag_tools）内部捕获 DiagnosticError 返回友好提示——诊断命令
  失败只是「查询失败」可读返回，不中断 Agent；
- 顶层 import langchain 无碍：本模块只会在构建 Agent（assistant 工厂）时被导入。

扩展方式：监控工具 = build_tools 里写 @tool 追加；只读诊断 = config/diagnostics.yaml 加命令。
"""
from __future__ import annotations

import logging

from langchain.tools import tool
from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from src.core import capabilities
from src.interfaces.diagnostic import (
    CommandSpec,
    DiagnosticError,
    DiagnosticSource,
    render_diag,
)
from src.interfaces.monitor import MonitorSource

logger = logging.getLogger("ops_agent.tools")


def build_tools(monitor: MonitorSource) -> list:
    """按注入的 MonitorSource 构建监控工具列表（模型按需调用取数）。

    四个 @tool 只做「暴露给模型 + 闭包注入 monitor」的薄接线；查询与格式化的
    真正实现收编在 src/core/capabilities.py——Agent 工具与关键词兜底（core/chat.py）
    转发同一批纯函数，因此无论是否走 LLM，返回的都是同一套规范文案。

    Args:
        monitor: 监控源实现（接口 MonitorSource），如 PrometheusMonitor / FakeMonitor
    Returns:
        四个 @tool 的列表：cpu_usage / memory_usage / disk_usage / active_alerts
    """
    if monitor is None:
        raise ValueError("build_tools 需要传入 monitor 参数（MonitorSource 实现）")

    @tool
    def cpu_usage() -> str:
        """查询当前 CPU 使用率（%）。

        Args:
            无

        Returns:
            如 "CPU 使用率：45.2%"；Prometheus 不可达或无数据时返回"查询失败/暂无数据"说明

        Examples:
            cpu_usage() 返回 "CPU 使用率：45.2%"
        """
        return capabilities.cpu_usage_text(monitor)

    @tool
    def memory_usage() -> str:
        """查询当前内存使用率（%）。

        Args:
            无

        Returns:
            如 "内存使用率：72.3%"；Prometheus 不可达或无数据时返回"查询失败/暂无数据"说明

        Examples:
            memory_usage() 返回 "内存使用率：72.3%"
        """
        return capabilities.memory_usage_text(monitor)

    @tool
    def disk_usage() -> str:
        """查询各挂载点的磁盘使用率（%），按使用率从高到低排列。

        Args:
            无

        Returns:
            如 "磁盘使用率：/ 45.2%，/data 88.1%"；无数据时返回"暂无数据"说明

        Examples:
            disk_usage() 返回 "磁盘使用率：/ 45.2%，/data 88.1%"
        """
        return capabilities.disk_usage_text(monitor)

    @tool
    def active_alerts() -> str:
        """查询当前处于 pending / firing 状态的告警。

        Args:
            无

        Returns:
            无活动告警时返回 "当前没有 pending/firing 的告警"；
            有则逐条列出告警名、级别、实例与状态

        Examples:
            active_alerts() 返回 "当前告警（firing 1 条 / pending 0 条）：[▲ firing] HighCPUUsage ..."
        """
        return capabilities.active_alerts_text(monitor)

    return [cpu_usage, memory_usage, disk_usage, active_alerts]


# ---------------------------------------------------------------------------
# 只读诊断工具（按 config/diagnostics.yaml 白名单动态生成）
# ---------------------------------------------------------------------------
def _tool_args_model(spec: CommandSpec):
    """按参数声明建 pydantic args schema（int/str + 默认值；不做取值约束）。

    取值边界（正则 / min / max）只在服务端 commands.build_command 校验，这里保持薄，
    规避 pydantic v1/v2 Field 约束差异；无默认值 = 必填。
    """
    fields = {}
    for param in spec.params:
        ann = int if param.type == "int" else str
        if param.default is not None:
            fields[param.name] = (ann, Field(default=param.default, description=param.description))
        else:
            fields[param.name] = (ann, Field(description=param.description))  # 必填
    return create_model(f"DiagArgs_{spec.key}", **fields)


def _tool_description(spec: CommandSpec) -> str:
    """生成工具 description：语义 + 参数清单；明确「只读，不改动系统」。"""
    text = spec.summary or spec.key
    parts = []
    for param in spec.params:
        if param.default is None:
            parts.append(f"{param.name}: 必填 {param.type}")
        else:
            parts.append(f"{param.name}: 可选 {param.type}，默认 {param.default!r}")
    if parts:
        text += "。" + "；".join(parts) + "。"
    text += "（只读诊断命令，不会改动系统，凭据无需你提供）"
    return text


def _make_diag_tool(source: DiagnosticSource, spec: CommandSpec):
    """把一条白名单诊断命令包成 LangChain 工具：source.run(key, params) → 可读文本。"""

    def _run(**values) -> str:
        try:
            result = source.run(spec.key, params=values)
        except DiagnosticError as exc:
            logger.warning("Agent 诊断工具 %s 调用失败: %s", spec.key, exc)
            return f"诊断命令执行失败：{exc}"
        return render_diag(result)

    return StructuredTool.from_function(
        func=_run,
        name=spec.key,
        description=_tool_description(spec),
        args_schema=_tool_args_model(spec),
    )


def build_diag_tools(diag: DiagnosticSource | None) -> list:
    """按注入的 DiagnosticSource 的命令清单生成只读诊断工具；diag 为 None 返回空列表。

    Args:
        diag: 只读诊断源（接口 DiagnosticSource），如 SshDiagnostics / FakeDiagnostics
    Returns:
        每条约一条工具的列表；工具名 = 命令 key，参数 schema 由命令参数声明生成
    """
    if diag is None:
        return []
    return [_make_diag_tool(diag, spec) for spec in diag.commands()]


# ---------------------------------------------------------------------------
# 动作执行工具位（预留，默认不注册）
# ---------------------------------------------------------------------------
# 说明：把重启服务 / 执行命令等动作包成 @tool 后，Agent 就能「自主执行恢复动作」。
# 出于安全考虑默认不启用，原因：
#   1) 执行凭据（host/密码/私钥）应来自规则配置，而不是让模型自由捏造参数；
#   2) 自动"改系统"风险高，启用前建议配合 create_agent 的 interrupt_before 做人工审批，
#      或只对受控主机开放。
# 需要启用时，参考下面的示例把 @tool 写出来，再追加进 build_tools 的返回列表即可。
#
# @tool
# def run_remediation(action_name: str, host: str, command: str) -> str:
#     """在指定主机上执行一条恢复命令（高危操作，需人工确认后再调用）。"""
#     ...
#
# 更推荐的做法：把动作做成「规则-动作」式白名单，由人工在 config/rules.yaml 里声明，
# 而不是让 LLM 自由选择（见 README「安全说明」）。
#
# 边界区分：上面 build_diag_tools 暴露的「只读诊断」走的是另一条低危通道——命令白名单
# 在 config/diagnostics.yaml 声明、参数服务端校验、恒不 sudo、只读不改系统，因此允许
# Agent 直接调用；本预留位指的还是「会改系统的处置动作」，接入前仍需 interrupt_before 审批。
