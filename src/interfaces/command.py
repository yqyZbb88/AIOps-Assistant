"""命令元信息契约（中性层）：白名单命令对 Agent 可见的只读信息。

只暴露命令名、说明和参数声明，不包含主机、凭据或执行实现。
本模块不依赖第三方，被只读诊断和写命令边界共同复用。
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
