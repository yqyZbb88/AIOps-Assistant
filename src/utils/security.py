"""递归结构化与自由文本敏感信息脱敏。"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import fields, is_dataclass
from typing import Any, Mapping

MASK = "***"
_KEY = re.compile(r"password|passwd|passphrase|token|secret|api[_-]?key|authorization|credential", re.I)
_PEM = re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----", re.I | re.S)
_AUTH = re.compile(r"(?i)(\bauthorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;]+")
_PAIR = re.compile(r"(?i)(\b(?:password|passwd|passphrase|token|secret|api[_-]?key)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)")
_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)")
_QUERY = re.compile(r"(?i)([?&](?:password|passwd|passphrase|token|secret|api[_-]?key)=)[^&#\s]*")
_lock = threading.RLock()
_known: set[str] = set()


def _is_sensitive_key(key: Any) -> bool:
    return bool(_KEY.search(str(key)))


def register_secrets(*values: Any) -> None:
    """注册需要在任意自由文本中精确遮蔽的值。"""
    with _lock:
        for value in values:
            text = "" if value is None else str(value)
            if len(text) >= 4 and text != MASK:
                _known.add(text)


def configure_secrets(*, auth_tokens: str | None = None) -> None:
    """从 API Token 和受支持环境变量注册已知密钥。"""
    try:
        tokens = json.loads(auth_tokens or "{}")
    except (TypeError, json.JSONDecodeError):
        tokens = {}
    if isinstance(tokens, Mapping):
        register_secrets(*tokens.keys())
    register_secrets(os.getenv("MODEL_API_KEY"))
    try:
        extra = json.loads(os.getenv("OPS_REDACTION_SECRETS", "[]"))
    except json.JSONDecodeError:
        extra = []
    if isinstance(extra, list):
        register_secrets(*extra)


def redact_text(value: Any) -> str:
    """脱敏常见凭据语法、私钥块和已注册的精确值。"""
    text = str(value)
    text = _PEM.sub(MASK, text)
    text = _AUTH.sub(lambda m: m.group(1) + MASK, text)
    text = _PAIR.sub(lambda m: m.group(1) + MASK, text)
    text = _USERINFO.sub(lambda m: m.group(1) + MASK + m.group(2), text)
    text = _QUERY.sub(lambda m: m.group(1) + MASK, text)
    with _lock:
        secrets = sorted(_known, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, MASK)
    return text


def redact_sensitive(data: Any, *, _sensitive: bool = False) -> Any:
    """递归返回脱敏副本；敏感键的整个值直接替换为掩码。"""
    if _sensitive:
        return MASK
    if isinstance(data, str):
        return redact_text(data)
    if isinstance(data, Mapping):
        return {key: redact_sensitive(value, _sensitive=_is_sensitive_key(key)) for key, value in data.items()}
    if isinstance(data, list):
        return [redact_sensitive(value) for value in data]
    if isinstance(data, tuple):
        return tuple(redact_sensitive(value) for value in data)
    if isinstance(data, set):
        return {redact_sensitive(value) for value in data}
    if is_dataclass(data) and not isinstance(data, type):
        return {field.name: redact_sensitive(getattr(data, field.name), _sensitive=_is_sensitive_key(field.name)) for field in fields(data)}
    return data
