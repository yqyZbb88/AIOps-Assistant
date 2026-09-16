"""Agent 写命令目录：config/agent_actions.yaml 解析（纯，可离线单测）。

这是**第三条**命令目录边界（前两条：diagnostics.yaml 只读、actions.yaml 规则自动处置），
与 src/plugins/diagnostics/commands.py 外壳同构——属**有意重复**：交接文档「明确不做 P2-1」
已判定抽象配置外壳收益中等、回归风险不小，故此处不引入新抽象，只复用真正的共享内核
src/plugins/common/cmddef.py（模板/参数/校验/转义）。

与另两条边界的差异：
1) allow_sudo=True —— 写命令的 sudo 由管理员逐条声明（与 actions.yaml 一致）；
2) 多一个 approval 段，且**只接受 timeout_seconds / max_pending 两个键**——
   多写任何键（尤其 enabled）都判为配置错误并关闭能力。这是「审批不可关闭」的绊线：
   配置层根本不存在关掉审批的开关。

本模块顶层不 import langchain / langgraph / paramiko / yaml（yaml 惰性导入），可离线单测。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from src.interfaces.agent_action import AgentActionError
from src.interfaces.command import CommandSpec, ParamSpec
from src.plugins.common import cmddef as _cmddef
from src.plugins.common.cmddef import CmdDef, CmdDefError, ParamError
from src.utils.config_loader import ROOT_DIR, interpolate

logger = logging.getLogger("aiops_assistant.agent_actions.catalog")

# 默认配置文件：<仓库根>/config/agent_actions.yaml（可用 OPS_AGENT_ACTIONS_FILE 覆盖）
DEFAULT_AGENT_ACTIONS_FILE: Path = ROOT_DIR / "config" / "agent_actions.yaml"

#: 统一指标目录可能生成的 Agent 工具名；无论当前是否启用，写命令一律不得重名。
from src.metrics.catalog import METRIC_CATALOG, tool_name

RESERVED_AGENT_TOOL_NAMES: tuple[str, ...] = tuple(
    tool_name(definition) for definition in METRIC_CATALOG
)

#: approval 段唯一允许出现的键（写入即配置错误——「关掉审批」在配置层不存在）
_ALLOWED_APPROVAL_KEYS: frozenset[str] = frozenset({"timeout_seconds", "max_pending"})

DEFAULT_APPROVAL_TIMEOUT_SECONDS = 900
DEFAULT_APPROVAL_MAX_PENDING = 32


class AgentActionsConfigError(ValueError):
    """agent_actions.yaml 结构 / 校验失败（视为目录无效，关闭本能力并记日志）。"""


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AgentActionsTarget:
    """目标主机（连接凭据只在此处，构造后留在实例内，模型上下文永不出现）。"""

    host: str = ""
    port: int = 22
    username: str = "root"
    password: str = ""
    key_filename: str = ""
    known_hosts: str = ""
    connect_timeout: int = 8


@dataclass(frozen=True)
class ApprovalPolicy:
    """审批策略——**不含开关**（审批恒为强制），只有容量与时限。"""

    timeout_seconds: int = DEFAULT_APPROVAL_TIMEOUT_SECONDS
    max_pending: int = DEFAULT_APPROVAL_MAX_PENDING


@dataclass(frozen=True)
class AgentActionsConfig:
    enabled: bool = True
    target: AgentActionsTarget = AgentActionsTarget()
    output_limit: int = 4000
    approval: ApprovalPolicy = ApprovalPolicy()
    commands: tuple[CmdDef, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# YAML 解析 + config 级结构校验
# ---------------------------------------------------------------------------
def _as_str(value: Any, what: str) -> str:
    if value is None:
        raise AgentActionsConfigError(f"{what} 不能为空")
    return str(value).strip()


def _as_int(value: Any, what: str, *, minimum: int | None = None) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise AgentActionsConfigError(f"{what} 必须是整数，得到: {value!r}")
    if minimum is not None and num < minimum:
        raise AgentActionsConfigError(f"{what} 不能小于 {minimum}")
    return num


def _parse_target(data: Any) -> AgentActionsTarget:
    if not isinstance(data, Mapping):
        raise AgentActionsConfigError("target 必须是映射（host/port/username/password 等）")
    host = _as_str(data.get("host"), "target.host")
    if not host:
        raise AgentActionsConfigError("target.host 不能为空（需与 .env 中 SSH 密码变量对应的主机）")
    password = data.get("password", "") or ""
    key_filename = data.get("key_filename", "") or ""
    known_hosts = data.get("known_hosts", "") or ""
    if not password and not key_filename:
        logger.warning(
            "agent_actions.target 未配置 password/key_filename，连接将走 agent/默认密钥或失败"
        )
    return AgentActionsTarget(
        host=host,
        port=_as_int(data.get("port", 22), "target.port", minimum=1),
        username=_as_str(data.get("username", "root"), "target.username") or "root",
        password=str(password),
        key_filename=str(key_filename),
        known_hosts=str(known_hosts),
        connect_timeout=_as_int(data.get("connect_timeout", 8), "target.connect_timeout", minimum=1),
    )


def _parse_approval(data: Any) -> ApprovalPolicy:
    """解析 approval 段：**只**接受 timeout_seconds / max_pending，多键即报错。

    刻意不接受任何开关键——审批是硬性语义，不允许经配置关闭。
    """
    if data is None:
        return ApprovalPolicy()
    if not isinstance(data, Mapping):
        raise AgentActionsConfigError("approval 必须是映射（timeout_seconds / max_pending）")
    unknown = set(data) - _ALLOWED_APPROVAL_KEYS
    if unknown:
        raise AgentActionsConfigError(
            "approval 段不支持键: " + ", ".join(sorted(unknown))
            + "（只允许 timeout_seconds / max_pending；审批不可关闭，没有开关）"
        )
    return ApprovalPolicy(
        timeout_seconds=_as_int(
            data.get("timeout_seconds", DEFAULT_APPROVAL_TIMEOUT_SECONDS),
            "approval.timeout_seconds", minimum=1,
        ),
        max_pending=_as_int(
            data.get("max_pending", DEFAULT_APPROVAL_MAX_PENDING),
            "approval.max_pending", minimum=1,
        ),
    )


def _parse_command(key: str, spec: Any) -> CmdDef:
    """逐命令委托 common.cmddef；写命令允许 sudo（管理员可信配置）。"""
    try:
        return _cmddef.parse_command_def(key, spec, allow_sudo=True)
    except CmdDefError as exc:
        raise AgentActionsConfigError(str(exc)) from exc


def build_config(data: Mapping[str, Any]) -> AgentActionsConfig:
    if not isinstance(data, Mapping):
        raise AgentActionsConfigError("Agent 写命令目录顶层必须是映射")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise AgentActionsConfigError("enabled 必须是布尔值")

    output_limit = _as_int(data.get("output_limit", 4000), "output_limit", minimum=1)
    target = _parse_target(data.get("target")) if enabled else AgentActionsTarget()
    approval = _parse_approval(data.get("approval")) if enabled else ApprovalPolicy()

    raw_cmds = data.get("commands") or {}
    if enabled and not isinstance(raw_cmds, Mapping):
        raise AgentActionsConfigError("commands 必须是映射")
    commands = tuple(_parse_command(key, spec) for key, spec in raw_cmds.items())
    if enabled and not commands:
        logger.warning("agent_actions.yaml 未声明任何 commands，Agent 写命令目录为空")
    return AgentActionsConfig(
        enabled=enabled,
        target=target,
        output_limit=output_limit,
        approval=approval,
        commands=commands,
    )


def parse_agent_actions(text: str) -> AgentActionsConfig:
    """把 yaml 文本（含环境变量占位）解析为 AgentActionsConfig；结构非法抛错。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 核心依赖缺失路径
        raise AgentActionsConfigError("缺少可选依赖 pyyaml，无法解析 Agent 写命令目录") from exc
    data = yaml.safe_load(interpolate(text)) or {}
    return build_config(data)


def load_agent_actions_config(path: str | Path | None = None) -> AgentActionsConfig | None:
    """读取 config/agent_actions.yaml；文件缺失 / enabled=false / 解析失败 → None（能力关闭）。"""
    file_path = Path(path) if path else Path(
        os.getenv("OPS_AGENT_ACTIONS_FILE", DEFAULT_AGENT_ACTIONS_FILE)
    )
    if not file_path.is_file():
        logger.info("Agent 写命令目录文件不存在，本能力关闭: %s", file_path)
        return None
    try:
        config = parse_agent_actions(file_path.read_text(encoding="utf-8"))
    except AgentActionsConfigError as exc:
        logger.error("Agent 写命令目录校验失败，本能力关闭: %s", exc)
        return None
    except Exception as exc:
        logger.error("Agent 写命令目录解析失败（%s），本能力关闭", exc)
        return None
    if not config.enabled:
        logger.info("agent_actions.enabled=false，Agent 写命令能力关闭")
        return None
    if not config.target.host:
        logger.warning("agent_actions.target.host 为空，Agent 写命令能力关闭")
        return None
    logger.info(
        "已加载 Agent 写命令目录: %d 条: %s（审批强制开启）",
        len(config.commands),
        [f"{c.key}(sudo={c.sudo})" for c in config.commands],
    )
    return config


# ---------------------------------------------------------------------------
# 工具名冲突检测（纯函数）
# ---------------------------------------------------------------------------
def colliding_keys(action_keys: Iterable[str], occupied: Iterable[str]) -> frozenset[str]:
    """返回 action_keys 与 occupied 的交集。

    create_agent 对重名工具**不报错**（静默遮蔽，见 2026-09-10 实测），
    因此必须在装配前显式检测：写命令 key 不能与监控工具名或只读诊断命令名冲突。
    """
    return frozenset(action_keys) & frozenset(occupied)


# ---------------------------------------------------------------------------
# 运行期：委托 common.cmddef，把中性异常翻译为写边界异常
# ---------------------------------------------------------------------------
def validated_params(command: CmdDef, values: Mapping[str, Any] | None) -> dict[str, Any]:
    """规约参数（默认/类型/min-max/全匹配），非法抛 AgentActionError（含参数名与原因）。"""
    try:
        return _cmddef.validated_params(command, values)
    except ParamError as exc:
        raise AgentActionError(str(exc)) from exc


def build_command(command: CmdDef, values: Mapping[str, Any] | None) -> str:
    """校验参数后构建安全命令串；参数非法抛 AgentActionError。"""
    try:
        return _cmddef.build_command(command, values)
    except ParamError as exc:
        raise AgentActionError(str(exc)) from exc


# ---------------------------------------------------------------------------
# 暴露给工具层 / 接口的元信息
# ---------------------------------------------------------------------------
def spec_for_command(command: CmdDef) -> CommandSpec:
    """CmdDef → interfaces 层的 CommandSpec（供工具层建 Agent 工具 schema）。"""
    params = tuple(
        ParamSpec(
            name=p.name,
            type=p.type,
            required=p.required,
            default=p.default,
            pattern=p.pattern,
            minimum=p.minimum,
            maximum=p.maximum,
            description=p.description,
        )
        for p in command.params
    )
    return CommandSpec(key=command.key, summary=command.summary, params=params)


def command_specs(config: AgentActionsConfig) -> tuple[CommandSpec, ...]:
    return tuple(spec_for_command(cmd) for cmd in config.commands)
