"""运维工具集。

四块能力都封装成 LangChain 工具交给 Agent 按需调用：
1) `build_tools(monitor)`：读监控数据（CPU / 内存 / 磁盘 / 告警），闭包注入 MonitorSource；
2) `build_diag_tools(diag)`：只读主机诊断（free / df / ps / systemctl / journalctl），
   命令白名单在 config/diagnostics.yaml 声明，工具按白名单动态生成——Agent 只选命令名 +
   受控语义参数，凭据 / 自由命令永不进模型上下文；
3) `build_inspection_tools(store)`：查询已归档的异常巡检轮次，结果明确标注为历史数据；
4) `build_agent_action_tools(source)`：**会改动系统**的写命令（config/agent_actions.yaml），
   与 2) 的区别是它返回 WriteToolBundle——工具与人工审批策略成对产出，无法只取其一。

设计约束：
- 每个工具返回 str，docstring 写清 Args / Returns / Examples；
- 监控工具（build_tools）是**薄接线**：查询+格式化的真正实现收编在
  src/core/capabilities.py（无 langchain 纯函数，Agent 工具与关键词兜底共用同一套）；
  只读诊断工具（build_diag_tools）内部捕获 DiagnosticError 返回友好提示——诊断命令
  失败只是「查询失败」可读返回，不中断 Agent；
- 写命令工具（build_agent_action_tools）捕获 AgentActionError 转可读文本；它**不会**
  在模型调用时执行——HumanInTheLoopMiddleware 先中断，人工批准后才真正执行；
- 顶层 import langchain 无碍：本模块只会在构建 Agent（assistant 工厂）时被导入。

扩展方式：监控工具 = build_tools 里写 @tool 追加；只读诊断 = config/diagnostics.yaml 加命令；
Agent 写命令 = config/agent_actions.yaml 加命令（改完需重启进程）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Literal

from langchain.tools import tool
from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from src.core import capabilities
from src.interfaces.agent_action import (
    AgentActionError,
    AgentActionSource,
    render_agent_action,
)
from src.interfaces.command import CommandSpec
from src.interfaces.diagnostic import (
    DiagnosticError,
    DiagnosticSource,
    render_diag,
)
from src.interfaces.inspection import InspectionError, InspectionStore
from src.interfaces.metric import MetricDefinition, MetricsConfig
from src.interfaces.monitor import MonitorSource
from src.interfaces.host import MonitorTarget
from src.plugins.monitors.prometheus_catalog import METRIC_CATALOG, tool_name
from src.utils.freshness import freshness_note
from src.utils.metrics_config import default_metrics_config

logger = logging.getLogger("aiops_assistant.tools")


def build_tools(
    monitor: MonitorSource,
    metrics_config: MetricsConfig | None = None,
    *,
    catalog: tuple[MetricDefinition, ...] = METRIC_CATALOG,
    target: MonitorTarget | None = None,
) -> list:
    """按注入监控源和 metrics.yaml 的 chat surface 动态生成只读工具。"""
    if monitor is None:
        raise ValueError("build_tools 需要传入 monitor 参数（MonitorSource 实现）")
    config = metrics_config or default_metrics_config()
    definitions = {definition.metric_id: definition for definition in catalog}
    tools: list = []

    for runtime in config.selected("chat"):
        definition = definitions.get(runtime.metric_id)
        if definition is None:
            continue

        def _run(_definition=definition) -> str:
            return capabilities.metric_text(
                monitor, _definition, with_freshness=True, target=target
            )

        tools.append(
            StructuredTool.from_function(
                func=_run,
                name=tool_name(definition),
                description=(
                    f"查询{definition.display_name}，单位 {definition.unit}。"
                    f"指标分组：{definition.group}。"
                    "Prometheus 不可达或无数据时返回可读的失败说明。"
                ),
            )
        )
    return tools

# ---------------------------------------------------------------------------
# 异常巡检归档工具（只读 InspectionStore，不直接访问 SQLite）
# ---------------------------------------------------------------------------
def _render_inspection_anomalies(records) -> str:
    lines = ["[巡检异常归档]"]
    if not records:
        lines.append("没有符合条件的异常巡检记录。")
        lines.append(
            freshness_note(
                source="inspection_runs",
                historical=True,
                detail="未查询到记录不代表当前系统正常；需要当前状态时请重新查询实时指标。",
            )
        )
        return "\n".join(lines)

    lines.append(f"记录数：{len(records)}")
    for record in records:
        lines.append(
            f"- id={record.record_id} checked_at={record.checked_at} level={record.level}"
        )
        if record.error:
            lines.append(f"  error: {record.error}")
        for finding in record.findings[:20]:
            lines.append(
                f"  - {finding.metric} [{finding.level}]: {finding.message}"
            )
        if len(record.findings) > 20:
            lines.append("  - ... 其余异常明细已省略")
    lines.append(
        freshness_note(
            source="inspection_runs",
            observed_at=records[0].checked_at,
            historical=True,
            detail="这些是历史异常轮次，不代表当前状态；需要当前状态时请重新查询实时指标。",
        )
    )
    return "\n".join(lines)


def build_inspection_tools(
    store: InspectionStore | None, *, host_id: str | None = None
) -> list:
    """生成异常巡检归档查询工具；存储不可用时返回空列表。"""
    if store is None:
        return []

    def _run(
        limit: int = 10,
        level: Literal["", "warning", "critical", "error"] = "",
    ) -> str:
        safe_limit = max(1, min(int(limit), 50))
        try:
            records = (
                store.recent(limit=min(safe_limit * 5, 250))
                if host_id is None
                else store.recent(
                    limit=min(safe_limit * 5, 250), host_id=host_id
                )
            )
        except InspectionError as exc:
            return (
                f"查询异常巡检归档失败：{exc}\n"
                + freshness_note(source="inspection_runs", historical=True)
            )
        if level:
            records = [record for record in records if record.level == level]
        return _render_inspection_anomalies(records[:safe_limit])

    return [
        StructuredTool.from_function(
            func=_run,
            name="inspection_anomalies",
            description=(
                "查询最近的异常巡检归档，可按 warning/critical/error 过滤。"
                "结果属于历史记录，不代表当前状态；需要当前状态时应重新查询实时指标。"
            ),
        )
    ]


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


def _tool_description(spec: CommandSpec, *, kind: str = "read") -> str:
    """生成工具 description：语义 + 参数清单。

    kind="read"（默认）：只读诊断工具，措辞保持原样（有测试逐字断言）；
    kind="write"：Agent 写命令工具，必须让模型明白「调用≠执行，要等人工审批」。
    """
    text = spec.summary or spec.key
    parts = []
    for param in spec.params:
        if param.default is None:
            parts.append(f"{param.name}: 必填 {param.type}")
        else:
            parts.append(f"{param.name}: 可选 {param.type}，默认 {param.default!r}")
    if parts:
        text += "。" + "；".join(parts) + "。"
    if kind == "write":
        text += (
            "（写命令，会改动目标系统；调用后将进入人工审批，"
            "审批通过前不会执行，因此不要在被批准前声称已执行；凭据无需你提供）"
        )
    else:
        text += "（只读诊断命令，不会改动系统，凭据无需你提供）"
    return text


def _make_diag_tool(source: DiagnosticSource, spec: CommandSpec):
    """把一条白名单诊断命令包成 LangChain 工具：source.run(key, params) → 可读文本。"""

    def _run(**values) -> str:
        try:
            result = source.run(spec.key, params=values)
        except DiagnosticError as exc:
            logger.warning("Agent 诊断工具 %s 调用失败: %s", spec.key, exc)
            return (
                f"诊断命令执行失败：{exc}\n"
                + freshness_note(source="SSH 只读诊断")
            )
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
# Agent 写命令工具（强制人工审批，默认关闭）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class WriteToolBundle:
    """写工具栏 + 配套的审批策略，**成对产出**。

    刻意不做成「返回工具列表，审批另行配置」：那样调用方可以只拿工具不要审批，
    从而绕过人工审核。把 interrupt_on 绑在同一个返回值里，使「有写工具必有审批」
    成为结构上的事实，而不是靠约定。
    """

    tools: list
    interrupt_on: dict


def _make_write_tool(source: AgentActionSource, spec: CommandSpec):
    """把一条白名单写命令包成 LangChain 工具：source.run(key, params) → 可读文本。

    与只读诊断工具的区别仅在于异常类型与渲染文案；args schema 同样**只含语义参数**，
    绝不生成 host/port/username/password 等连接字段。
    """

    def _run(**values) -> str:
        try:
            result = source.run(spec.key, params=values)
        except AgentActionError as exc:
            logger.warning("Agent 写命令工具 %s 调用失败: %s", spec.key, exc)
            return (
                f"写命令执行失败：{exc}\n"
                + freshness_note(source="SSH 写命令执行")
            )
        return render_agent_action(result)

    return StructuredTool.from_function(
        func=_run,
        name=spec.key,
        description=_tool_description(spec, kind="write"),
        args_schema=_tool_args_model(spec),
    )


def build_agent_action_tools(
    source: AgentActionSource | None,
    *,
    reserved: Iterable[str] = (),
) -> WriteToolBundle:
    """按注入的 AgentActionSource 生成写命令工具 + 审批策略；source 为 None 返回空。

    Args:
        source: 写命令源（接口 AgentActionSource），如 SshAgentActionSource；None = 该能力关闭
        reserved: 已被占用的工具名（监控工具名 + 只读诊断命令名）。命中的命令被丢弃并记
            ERROR——create_agent 对重名工具**不报错**（静默遮蔽），必须在这里拦住。
    Returns:
        WriteToolBundle(tools, interrupt_on)。interrupt_on 恒为
        {工具名: {"allowed_decisions": ["approve", "reject"]}}——**永不提供 edit**，
        因为允许编辑参数等于绕过管理员在配置里声明的模板与约束。
    """
    if source is None:
        return WriteToolBundle(tools=[], interrupt_on={})

    occupied = set(reserved)
    tools: list = []
    interrupt_on: dict = {}
    for spec in source.commands():
        if spec.key in occupied:
            logger.error(
                "Agent 写命令 %r 与已占用的工具名冲突，已丢弃该命令（重名工具会被静默遮蔽）",
                spec.key,
            )
            continue
        tools.append(_make_write_tool(source, spec))
        # 恒 approve/reject：不给 edit，也不给「不中断」
        interrupt_on[spec.key] = {"allowed_decisions": ["approve", "reject"]}
    return WriteToolBundle(tools=tools, interrupt_on=interrupt_on)


# 边界区分：
#   - build_tools        —— 监控读数，只读，Agent 可直接调用；
#   - build_diag_tools   —— 只读诊断（config/diagnostics.yaml，恒不 sudo、不改系统），可直接调用；
#   - build_agent_action_tools（上方）—— 会改系统的写命令（config/agent_actions.yaml），
#     必须经 HumanInTheLoopMiddleware 人工审批后才执行，且与 rules.yaml 的无头自动处置
#     完全隔离（两者各用各的白名单，互不可见）。
