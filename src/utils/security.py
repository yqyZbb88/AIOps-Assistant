"""安全工具：敏感信息打码。

设计：
- 结构化记录中包含密钥类字段的 dict 应先过 `redact_sensitive()`。
- 当前实现只按顶层键名做浅层打码，不是通用文本脱敏器；自由文本、嵌套对象和异常信息
  仍需调用方避免写入敏感值。统一文本脱敏属于后续安全加固项。

注：参数必填校验不在本模块——host 由 plugins/common/endpoint.py 的
endpoint_from_params 校验，命令参数由 plugins/common/cmddef.py 的 validated_params 校验。
"""
from __future__ import annotations

from typing import Any

# 打码判定的键名子串（大小写不敏感，涵盖 password/token/secret/api_key）
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
