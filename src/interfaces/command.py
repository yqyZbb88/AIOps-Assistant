"""命令元信息契约（中性层）：白名单命令对 Agent 可见的「只读面」。

一条白名单命令（无论只读诊断还是写动作）暴露给工具层的信息只有：
命令名 + 说明 + 参数声明。**不包含**主机、凭据、执行实现——那些只活在插件实例里。

本模块为零依赖叶子（不 import 任何第三方），被 interfaces/diagnostic.py 与
interfaces/agent_action.py 共同复用，避免两条边界的类型定义漂移。

历史：ParamSpec / CommandSpec 原先定义在 interfaces/diagnostic.py（只读侧专用）；
2026-09-10 引入写动作边界时迁出到本模块，diagnostic.py 保留同名 re-export 以兼容既有引用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParamSpec:
    """命令参数声明（来自 yaml 的 params），供工具层建 schema。"""

    name: str
    type: str = "str"          # "str" | "int"
    required: bool = False
    default: Any = None
    pattern: str | None = None  # str 型参数的全匹配正则（服务端校验）
    minimum: int | None = None  # int 型参数下限
    maximum: int | None = None  # int 型参数上限
    description: str = ""


@dataclass(frozen=True)
class CommandSpec:
    """一条白名单命令的元信息（Agent 可见面）。"""

    key: str
    summary: str = ""
    params: tuple[ParamSpec, ...] = ()
