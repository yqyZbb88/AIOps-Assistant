"""安全工具：敏感信息打码 + 命令/动作参数必填校验。

设计：
- 记录 / 日志里任何包含密钥类字段的 dict 都应先过 redact_sensitive()，
  保证 password/token/secret/api_key 等永不落入日志与审计；
- 动作执行前用 require_params() 校验必填参数，缺参直接在进入 SSH/执行前拦截。
"""
from __future__ import annotations

from typing import Any, Iterable

# 打码判定的键名子串（大小写不敏感；涵盖旧 ops_bot 的 password/token/secret/api_key）
_SENSITIVE_SUBSTRINGS: tuple[str, ...] = ("password", "token", "secret", "api_key", "passwd")

# 打码后统一显示的掩码
MASK = "***"


def _is_sensitive_key(key: Any) -> bool:
    text = str(key).lower()
    return any(s in text for s in _SENSITIVE_SUBSTRINGS)


def redact_sensitive(data: dict[str, Any]) -> dict[str, Any]:
    """返回入参 dict 的浅拷贝，键名含敏感子串的字段值替换为 ***。

    用于记录 / 日志 / 审计：只返回副本，不改动原始参数（动作执行仍用原值）。
    嵌套字段（如 list/dict）暂不深入递归，按顶层键名打码整个值。
    """
    redacted = dict(data)
    for key in redacted:
        if _is_sensitive_key(key):
            redacted[key] = MASK
    return redacted


def require_params(
    params: dict[str, Any],
    required: Iterable[str],
    *,
    scope: str = "动作",
) -> None:
    """校验必填参数非空；缺失时抛 ValueError（由调用方收敛为业务异常）。

    用途：命令/主机等关键参数只能来自规则配置（rules.yaml），
    缺参 → 立即失败，不让半截参数进入 SSH 等副作用路径。
    """
    missing = [name for name in required if not params.get(name)]
    if missing:
        raise ValueError(f"{scope}缺少必填参数: {', '.join(missing)}")
