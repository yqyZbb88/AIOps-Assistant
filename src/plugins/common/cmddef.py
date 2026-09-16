"""白名单命令目录机制：模板 + 参数声明 + 校验 + 安全命令构建（纯函数，可离线单测）。

读（config/diagnostics.yaml，只读诊断）与写（config/actions.yaml，修复命令）两条边界
共用同一套目录 schema 与构建逻辑。**本模块不 import langchain / paramiko / yaml**，
只吃已解析的 dict，异常为中性 CmdDefError（配置期）/ ParamError（运行期参数），
由各边界映射为自己的异常（诊断 → DiagnosticsConfigError/DiagnosticError，
动作 → CmdDefError/ActionError）。

安全不变量（目录模板 = 管理员可信文本）：
- 命令串 = 模板 + 逐参 shell 转义的参数；模型/规则自由文本永不拼进 shell；
- 模板占位符 {name}：占位名集合必须**等于** params 声明集合
  （声明了没用到 / 模板出现未声明占位 → 配置失败，防歧义与打字错误静默放行）；
- 参数仅 int / str：str 必须声明 pattern 并在运行期 re.fullmatch 全匹配；
  int 强转并按 min/max 收敛；多余 / 缺失必填 → 拒绝；
- sudo 是否允许由目录性质决定（parse_command_def 的 allow_sudo 参数），本层只按标志记录。
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Mapping

# 占位符：{name}，name 限小写字母开头（避免把 `{...}` 当自由文本）
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
# 命令 key 命名规则（同时是 Agent 工具名 / 规则 action 引用名，须是合法标识符）
_COMMAND_KEY = re.compile(r"^[a-z][a-z0-9_]*$")


class CmdDefError(ValueError):
    """命令目录结构 / 校验失败（配置期，视为目录整体无效并关闭该目录）。"""


class ParamError(ValueError):
    """单次调用的参数校验失败（运行期：非法值 / 越权 / 缺失 / 多余）。"""


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CmdParam:
    """目录命令的参数声明。"""

    name: str
    type: str = "str"          # "str" | "int"
    required: bool = False
    default: Any = None
    pattern: str | None = None  # str 型参数的全匹配正则（运行期 re.fullmatch）
    minimum: int | None = None  # int 型参数下限（越界收敛到下限）
    maximum: int | None = None  # int 型参数上限（越界收敛到上限）
    description: str = ""


@dataclass(frozen=True)
class CmdDef:
    """目录里的一条命令。"""

    key: str
    summary: str = ""
    template: str = ""
    params: tuple[CmdParam, ...] = field(default_factory=tuple)
    sudo: bool = False          # 是否以 sudo 执行（读目录恒 False，由边界保证）


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def _as_int(value: Any, what: str, *, minimum: int | None = None) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        raise CmdDefError(f"{what} 必须是整数，得到: {value!r}")
    if minimum is not None and num < minimum:
        raise CmdDefError(f"{what} 不能小于 {minimum}")
    return num


def parse_param(name: str, spec: Any) -> CmdParam:
    """解析单条参数声明（str 必 pattern；int 用 min/max 收敛）；非法抛 CmdDefError。"""
    if not isinstance(spec, Mapping):
        raise CmdDefError(f"命令参数 {name} 必须是映射（type/pattern/min/max/…）")
    ptype = spec.get("type", "str")
    if ptype not in ("int", "str"):
        raise CmdDefError(f"命令参数 {name} 的 type 仅支持 int/str，得到: {ptype!r}")

    has_default = "default" in spec
    default = spec.get("default")
    required = spec.get("required")
    if required is None:
        required = not has_default  # 无默认值 → 视为必填

    description = str(spec.get("description", "") or "")

    if ptype == "str":
        pattern = spec.get("pattern")
        if not pattern:
            raise CmdDefError(f"str 参数 {name} 必须声明 pattern（re.fullmatch 全匹配）")
        try:
            re.compile(str(pattern))
        except re.error as exc:
            raise CmdDefError(f"str 参数 {name} 的 pattern 不是合法正则: {exc}") from exc
        return CmdParam(name=name, type="str", required=required, default=default,
                        pattern=str(pattern), description=description)
    # 整数参数
    minimum = spec.get("min")
    maximum = spec.get("max")
    if minimum is not None:
        minimum = _as_int(minimum, f"int 参数 {name}.min")
    if maximum is not None:
        maximum = _as_int(maximum, f"int 参数 {name}.max")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise CmdDefError(f"int 参数 {name}.min({minimum}) 大于 max({maximum})")
    return CmdParam(name=name, type="int", required=required, default=default,
                    minimum=minimum, maximum=maximum, description=description)


def template_param_order(template: str, declared: dict[str, CmdParam]) -> list[str]:
    """返回模板中占位符按出现先后排序的去重名单（参数顺序与模板一致，便于阅读）。"""
    return list(dict.fromkeys(name for name in _PLACEHOLDER.findall(template) if name in declared))


def parse_command_def(key: str, spec: Any, *, allow_sudo: bool = True) -> CmdDef:
    """解析目录里一条命令；模板/参数/占位一致性非法抛 CmdDefError。

    allow_sudo=False 时若声明 sudo: true 直接拒绝（只读目录不许提权）。
    """
    if not isinstance(spec, Mapping):
        raise CmdDefError(f"命令 {key} 必须是映射（summary/template/params）")
    if _COMMAND_KEY.match(key) is None:
        raise CmdDefError(f"命令名 {key!r} 不合法（须小写字母开头，仅 a-z0-9_）")

    template = str(spec.get("template", "") or "").strip()
    if not template:
        raise CmdDefError(f"命令 {key}.template 不能为空")

    raw_params = spec.get("params") or {}
    if not isinstance(raw_params, Mapping):
        raise CmdDefError(f"命令 {key}.params 必须是映射")

    declared = {name: parse_param(name, p) for name, p in raw_params.items()}
    used = {name for name in _PLACEHOLDER.findall(template)}
    extra = used - set(declared)          # 模板用到但未声明 → 配置错误
    unused = set(declared) - used         # 声明了但模板没用 → 无意义，防歧义也报错
    if extra:
        raise CmdDefError(
            f"命令 {key}.template 出现未声明的占位符: {', '.join(sorted(extra))}"
        )
    if unused:
        raise CmdDefError(
            f"命令 {key} 声明了参数但 template 未使用（多余）: {', '.join(sorted(unused))}"
        )

    sudo = bool(spec.get("sudo", False))
    if sudo and not allow_sudo:
        raise CmdDefError(f"命令 {key} 声明了 sudo: true，但该目录不允许提权（只读）")

    summary = str(spec.get("summary", "") or "")
    return CmdDef(
        key=key,
        summary=summary,
        template=template,
        params=tuple(declared[name] for name in template_param_order(template, declared)),
        sudo=sudo,
    )


# ---------------------------------------------------------------------------
# 运行期：参数规约 + 安全命令构建
# ---------------------------------------------------------------------------
def _validate_value(param: CmdParam, raw: Any) -> Any:
    """单参数校验/规约；非法抛 ParamError（含参数名与原因，便于调用方修正）。"""
    name = param.name
    if param.type == "int":
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ParamError(f"参数 {name} 需要整数，得到: {raw!r}")
        if param.minimum is not None and value < param.minimum:
            value = param.minimum
        if param.maximum is not None and value > param.maximum:
            value = param.maximum
        return value
    # 字符串参数
    value = str(raw)
    if param.pattern is not None and re.fullmatch(param.pattern, value) is None:
        raise ParamError(f"参数 {name} 含有非法字符（白名单正则校验不通过）")
    return value


def validated_params(cmd: CmdDef, values: Mapping[str, Any] | None) -> dict[str, Any]:
    """把调用方传入的参数规约为声明参数的具体值（默认 / 类型强转 / min-max / 全匹配）。

    - 未声明（多余）参数 → 拒绝；必填缺失 / 无默认又没给 → 拒绝；非法 → ParamError。
    返回 {参数名: 规约后值}（涵盖命令所有声明参数）。
    """
    supplied = dict(values or {})
    allowed = {p.name for p in cmd.params}
    unknown = set(supplied) - allowed
    if unknown:
        raise ParamError(f"命令 {cmd.key} 不接受参数: {', '.join(sorted(unknown))}")

    result: dict[str, Any] = {}
    for param in cmd.params:
        if param.name in supplied:
            result[param.name] = _validate_value(param, supplied[param.name])
        elif param.default is not None:
            result[param.name] = _validate_value(param, param.default)
        elif param.required:
            raise ParamError(f"命令 {cmd.key} 缺少必填参数: {param.name}")
        else:  # 可选、无默认、未提供
            raise ParamError(f"命令 {cmd.key} 缺少参数（无默认值）: {param.name}")
    return result


def build_command(cmd: CmdDef, values: Mapping[str, Any] | None) -> str:
    """校验参数后把每个 {name} 占位替换为 shell 安全字面量，返回最终命令字符串。

    - str 参数已按 pattern 全匹配 → 再 shlex.quote 双保险；int 直接转十进制；
    - 命令模板静态部分 + 已转义参数拼成单条命令，交给远端 sh 执行。
    """
    norm = validated_params(cmd, values)
    out = cmd.template
    for param in cmd.params:
        value = norm[param.name]
        token = "{" + param.name + "}"
        rendered = str(int(value)) if param.type == "int" else shlex.quote(str(value))
        out = out.replace(token, rendered)
    return out
