"""只读诊断配置适配层：diagnostics.yaml 解析（config 级）+ 命令目录机制委托 common/cmddef。

职责分层：
- 命令级「模板 + 参数 schema + 校验 + 构建」（通用机制）在 src/plugins/common/cmddef.py，
  读/写目录共用；本模块做只读侧适配：
    1) config 级（enabled/provider/target/output_limit/commands 顶层）仍在此解析；
    2) 逐命令调用 cmddef.parse_command_def(allow_sudo=False)，CmdDefError → DiagnosticsConfigError
       （只读目录**不允许 sudo**，声明 sudo: true 直接关闭能力）；
    3) 运行期 validated_params / build_command 委托 cmddef，把 ParamError 翻译为 DiagnosticError
       （工具层捕获后转可读文本，不中断 Agent）。

本模块 **不 import langchain / paramiko**（yaml 亦惰性导入），可离线单测。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from src.interfaces.diagnostic import CommandSpec, DiagnosticError, ParamSpec
from src.plugins.common import cmddef as _cmddef
from src.plugins.common.cmddef import CmdDef, CmdDefError, ParamError
from src.plugins.common.endpoint import DEFAULT_CONNECT_TIMEOUT
from src.utils.config_loader import ROOT_DIR, interpolate

logger = logging.getLogger("aiops_assistant.diagnostics.config")

# 默认配置文件：<仓库根>/config/diagnostics.yaml（可用 OPS_DIAGNOSTICS_FILE 覆盖）
DEFAULT_DIAGNOSTICS_FILE: Path = ROOT_DIR / "config" / "diagnostics.yaml"


class DiagnosticsConfigError(ValueError):
    """diagnostics.yaml 结构 / 校验失败（视为配置错误，关闭能力并记日志）。"""


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiagnosticsTarget:
    """目标主机（连接凭据只在此处，构造后留在实例内，模型上下文永不出现）。"""

    host: str = ""
    port: int = 22
    username: str = "root"
    password: str = ""
    key_filename: str = ""
    known_hosts: str = ""
    #: 连接与命令超时；默认值来自 SSH 层唯一的那处定义（见 common/endpoint.py）
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT


@dataclass(frozen=True)
class DiagnosticsConfig:
    enabled: bool = True
    provider: str = "ssh"
    target: DiagnosticsTarget = DiagnosticsTarget()
    output_limit: int = 4000
    commands: tuple[CmdDef, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# YAML 解析 + config 级结构校验
# ---------------------------------------------------------------------------
def _as_str(value: Any, what: str) -> str:
    if value is None:
        raise DiagnosticsConfigError(f"{what} 不能为空")
    return str(value).strip()


def _as_int(value: Any, what: str, *, minimum: int | None = None) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise DiagnosticsConfigError(f"{what} 必须是整数，得到: {value!r}")
    if minimum is not None and num < minimum:
        raise DiagnosticsConfigError(f"{what} 不能小于 {minimum}")
    return num


def _parse_target(data: Any) -> DiagnosticsTarget:
    if not isinstance(data, Mapping):
        raise DiagnosticsConfigError("target 必须是映射（host/port/username/password 等）")
    host = _as_str(data.get("host"), "target.host")
    if not host:
        raise DiagnosticsConfigError("target.host 不能为空（需与 .env 中 SSH 密码变量对应的主机）")
    password = data.get("password", "") or ""
    key_filename = data.get("key_filename", "") or ""
    known_hosts = data.get("known_hosts", "") or ""
    if not password and not key_filename:
        logger.warning("diagnostics.target 未配置 password/key_filename，连接将走 agent/默认密钥或失败")
    return DiagnosticsTarget(
        host=host,
        port=_as_int(data.get("port", 22), "target.port", minimum=1),
        username=_as_str(data.get("username", "root"), "target.username") or "root",
        password=str(password),
        key_filename=str(key_filename),
        known_hosts=str(known_hosts),
        connect_timeout=_as_int(
            data.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT),
            "target.connect_timeout",
            minimum=1,
        ),
    )


def _parse_command(key: str, spec: Any) -> CmdDef:
    """逐命令委托 common.cmddef；只读目录不允许 sudo，错误统一为 DiagnosticsConfigError。"""
    try:
        return _cmddef.parse_command_def(key, spec, allow_sudo=False)
    except CmdDefError as exc:
        raise DiagnosticsConfigError(str(exc)) from exc


def build_config(data: Mapping[str, Any]) -> DiagnosticsConfig:
    if not isinstance(data, Mapping):
        raise DiagnosticsConfigError("诊断配置顶层必须是映射")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise DiagnosticsConfigError("enabled 必须是布尔值")
    provider = _as_str(data.get("provider", "ssh"), "provider") or "ssh"
    output_limit = _as_int(data.get("output_limit", 4000), "output_limit", minimum=1)
    target = _parse_target(data.get("target")) if enabled else DiagnosticsTarget()

    raw_cmds = data.get("commands") or {}
    if enabled and not isinstance(raw_cmds, Mapping):
        raise DiagnosticsConfigError("commands 必须是映射")
    commands = tuple(
        command for command in (_parse_command(key, spec) for key, spec in raw_cmds.items())
        if command.enabled
    )
    if enabled and not commands:
        logger.warning("diagnostics.yaml 未声明任何 commands，只读诊断命令为空")
    return DiagnosticsConfig(
        enabled=enabled,
        provider=provider,
        target=target,
        output_limit=output_limit,
        commands=commands,
    )


def parse_diagnostics(text: str) -> DiagnosticsConfig:
    """把 yaml 文本（含 ${ENV} 占位）解析为 DiagnosticsConfig；结构非法抛 DiagnosticsConfigError。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 核心依赖缺失路径
        raise DiagnosticsConfigError("缺少可选依赖 pyyaml，无法解析诊断配置") from exc
    # 先做环境变量插值（target.password 的 ${SSH_PASSWORD_...} 在解析前替换，规则同 rules.yaml）
    data = yaml.safe_load(interpolate(text)) or {}
    return build_config(data)


def load_diagnostics_config(path: str | Path | None = None) -> DiagnosticsConfig | None:
    """读取 config/diagnostics.yaml；文件缺失 / enabled=false / 解析失败 → None（能力关闭）。"""
    file_path = Path(path) if path else Path(
        os.getenv("OPS_DIAGNOSTICS_FILE", DEFAULT_DIAGNOSTICS_FILE)
    )
    if not file_path.is_file():
        logger.info("诊断配置文件不存在，只读诊断能力关闭: %s", file_path)
        return None
    try:
        config = parse_diagnostics(file_path.read_text(encoding="utf-8"))
    except DiagnosticsConfigError as exc:
        logger.error("诊断配置校验失败，只读诊断能力关闭: %s", exc)
        return None
    except Exception as exc:
        logger.error("诊断配置解析失败（%s），只读诊断能力关闭", exc)
        return None
    if not config.enabled:
        logger.info("diagnostics.enabled=false，只读诊断能力关闭")
        return None
    if not config.target.host:
        logger.warning("diagnostics.target.host 为空，只读诊断能力关闭")
        return None
    logger.info(
        "已加载诊断配置: 命令 %d 条，目标 %s@%s",
        len(config.commands), config.target.username, config.target.host,
    )
    return config


# ---------------------------------------------------------------------------
# 运行期：委托 common.cmddef，把中性异常翻译为只读诊断异常
# ---------------------------------------------------------------------------
def validated_params(command: CmdDef, values: Mapping[str, Any] | None) -> dict[str, Any]:
    """规约参数（默认/类型/min-max/全匹配），非法抛 DiagnosticError（含参数名与原因）。"""
    try:
        return _cmddef.validated_params(command, values)
    except ParamError as exc:
        raise DiagnosticError(str(exc)) from exc


def build_command(command: CmdDef, values: Mapping[str, Any] | None) -> str:
    """校验参数后构建安全命令串；参数非法抛 DiagnosticError。"""
    try:
        return _cmddef.build_command(command, values)
    except ParamError as exc:
        raise DiagnosticError(str(exc)) from exc


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


def command_specs(config: DiagnosticsConfig) -> tuple[CommandSpec, ...]:
    return tuple(spec_for_command(cmd) for cmd in config.commands)
