"""写命令目录：config/actions.yaml 解析（纯，可离线单测）。

M1b 起「允许执行的修复命令」以目录形式集中声明（模板 + 参数 schema + sudo），
rules.yaml 的规则只引用目录 key 并提供语义参数；executor 按 key 找到目录项执行。

- 文件缺失 / enabled=false / 结构或校验失败 → 返回 None（自动处置关闭，服务照常）；
- 命令级解析委托 common/cmddef（allow_sudo=True：写命令允许管理员声明 sudo）；
- 密码等连接凭据**不在本文件**：由 rules.yaml 每条规则 params 提供（.env 占位）。

本模块顶层不 import langchain / paramiko / yaml（yaml 惰性导入）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from src.plugins.common.cmddef import CmdDef, CmdDefError, parse_command_def
from src.utils.config_loader import ROOT_DIR, interpolate

logger = logging.getLogger("aiops_assistant.actions.catalog")

# 默认目录文件：<仓库根>/config/actions.yaml（可用 OPS_ACTIONS_FILE 覆盖）
DEFAULT_ACTIONS_FILE: Path = ROOT_DIR / "config" / "actions.yaml"


class ActionsConfigError(ValueError):
    """actions.yaml 结构 / 校验失败（视为目录无效，关闭写命令目录）。"""


@dataclass(frozen=True)
class ActionsConfig:
    enabled: bool = True
    commands: tuple[CmdDef, ...] = field(default_factory=tuple)


def build_actions(data: Mapping[str, Any]) -> ActionsConfig:
    if not isinstance(data, Mapping):
        raise ActionsConfigError("动作目录顶层必须是映射")
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ActionsConfigError("enabled 必须是布尔值")

    raw_cmds = data.get("commands") or {}
    if enabled and not isinstance(raw_cmds, Mapping):
        raise ActionsConfigError("commands 必须是映射")
    commands: tuple[CmdDef, ...]
    try:
        # 写命令目录允许声明 sudo: true（管理员可信配置）
        commands = tuple(parse_command_def(key, spec, allow_sudo=True)
                         for key, spec in raw_cmds.items())
    except CmdDefError as exc:
        raise ActionsConfigError(str(exc)) from exc
    if enabled and not commands:
        logger.warning("actions.yaml 未声明任何 commands，写命令目录为空（自动处置将全部失效）")
    return ActionsConfig(enabled=enabled, commands=commands)


def parse_actions(text: str) -> ActionsConfig:
    """把 yaml 文本（含 ${ENV} 占位）解析为 ActionsConfig；结构非法抛 ActionsConfigError。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 核心依赖缺失路径
        raise ActionsConfigError("缺少可选依赖 pyyaml，无法解析动作目录") from exc
    data = yaml.safe_load(interpolate(text)) or {}
    return build_actions(data)


def load_actions_catalog(path: str | Path | None = None) -> ActionsConfig | None:
    """读取 config/actions.yaml；文件缺失 / enabled=false / 解析失败 → None（能力关闭）。"""
    file_path = Path(path) if path else Path(
        os.getenv("OPS_ACTIONS_FILE", DEFAULT_ACTIONS_FILE)
    )
    if not file_path.is_file():
        logger.info("动作目录文件不存在，写命令目录关闭: %s", file_path)
        return None
    try:
        config = parse_actions(file_path.read_text(encoding="utf-8"))
    except ActionsConfigError as exc:
        logger.error("动作目录校验失败，写命令目录关闭: %s", exc)
        return None
    except Exception as exc:
        logger.error("动作目录解析失败（%s），写命令目录关闭", exc)
        return None
    if not config.enabled:
        logger.info("actions.enabled=false，写命令目录关闭")
        return None
    logger.info(
        "已加载写命令目录: %d 条: %s",
        len(config.commands),
        [f"{c.key}(sudo={c.sudo})" for c in config.commands],
    )
    return config
