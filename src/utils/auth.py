"""共享密钥鉴权：密钥 → 身份 的**纯逻辑**。

只依赖 stdlib（`hmac` / `json`），**不 import FastAPI、不在 import 期读配置文件**：
- FastAPI 的接线在 `src/api/deps.py`，纯逻辑留在这里以便独立单测；
- 无第三方依赖的降级部署也要能照常 import（见 tests/test_no_langchain_import.py）。

密钥 → 身份 的映射来自 config/settings.yaml 的 `auth.tokens`（JSON 字符串原文），
真实值由环境变量 `OPS_API_TOKENS` 注入 .env，config/ 下不落任何明文。
"""
from __future__ import annotations

import hmac
import json
from functools import lru_cache


class AuthConfigError(Exception):
    """鉴权配置不可用：JSON 非法 / 不是映射 / 空键空值 / 启用了却没配任何密钥。"""


@lru_cache(maxsize=8)
def parse_tokens(raw: str | None) -> tuple[tuple[str, str], ...]:
    """把 JSON 的「密钥 → 身份」映射解析成有序序列；未配置（空串/None）→ 空元组。

    用元组而非 dict：可哈希（喂 lru_cache）、有序、不可变。

    `raw` 允许是 `None`：yaml 里写 `tokens:` 且不带值时，`_as_scalar` 会让 `None`
    一路穿到 dataclass —— 它只对「键缺失」应用 dataclass 默认值，不管「键存在但值为
    null」。settings.yaml 用单引号占位符（`'${...:-}'`）本来就能避免这种情况，
    这里再容错一次，免得配置方式一变就炸在 `None.strip()` 上。
    """
    text = (raw or "").strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthConfigError(f"auth.tokens 不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AuthConfigError('auth.tokens 必须是 JSON 对象（形如 {"密钥": "身份"}）')

    pairs: list[tuple[str, str]] = []
    for token, identity in data.items():
        if not isinstance(token, str) or not isinstance(identity, str):
            raise AuthConfigError("auth.tokens 的键与值都必须是字符串")
        if not token or not identity:
            # 空密钥会让「随便传个空 Bearer」就通过；空身份会让所有者校验失去意义
            raise AuthConfigError("auth.tokens 不允许空密钥或空身份")
        pairs.append((token, identity))
    return tuple(pairs)


@lru_cache(maxsize=8)
def parse_approvers(raw: str | None) -> tuple[str, ...]:
    """解析审批人身份 JSON 数组；空配置为 ()，结构不合法立即拒绝启动。"""
    text = (raw or "").strip()
    if not text:
        return ()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AuthConfigError(f"auth.approver_identities 不是合法 JSON: {exc}") from exc
    if not isinstance(data, list):
        raise AuthConfigError('auth.approver_identities 必须是 JSON 数组（形如 ["alice"]）')
    values: list[str] = []
    for identity in data:
        if not isinstance(identity, str) or not identity.strip():
            raise AuthConfigError("auth.approver_identities 只能包含非空字符串")
        value = identity.strip()
        if value not in values:
            values.append(value)
    return tuple(values)


def resolve_identity(
    tokens: tuple[tuple[str, str], ...], provided: str
) -> str | None:
    """按密钥解析身份；不匹配返回 `None`。

    **常数时间比对，且遍历全部密钥、不提前返回。** 提前 `return` 会让响应时间泄露
    「第几个密钥匹配上了」，也就泄露了「猜的密钥前缀对不对」。密钥数量是个位数，
    全遍历的代价可以忽略。

    `.encode()` 不能省：`hmac.compare_digest` 收到 `str` 时，**任一侧含非 ASCII 会
    直接抛 `TypeError`** —— 那会变成一个 500，而不是本该有的 401（已实测）。
    """
    provided_bytes = provided.encode("utf-8")
    matched: str | None = None
    for token, identity in tokens:
        if hmac.compare_digest(provided_bytes, token.encode("utf-8")):
            matched = identity
    return matched


def ensure_usable(
    enabled: bool,
    raw: str | None,
    *,
    approval_mode: str = "owner",
    approvers_raw: str | None = None,
) -> None:
    """启动期快速失败：鉴权或审批策略配置不确定时拒绝启动。"""
    if approval_mode not in {"owner", "approvers_only"}:
        raise AuthConfigError(
            "auth.approval_mode 只支持 owner / approvers_only，"
            f"得到: {approval_mode!r}"
        )
    if not enabled:
        return
    if not parse_tokens(raw):
        raise AuthConfigError(
            "auth.enabled 为 true，但 auth.tokens 解析不出任何密钥。"
            "请把密钥映射写进 .env 的 OPS_API_TOKENS，或把 auth.enabled 改回 false。"
        )
    if approval_mode == "approvers_only" and not parse_approvers(approvers_raw):
        raise AuthConfigError(
            "auth.approval_mode=approvers_only 时，"
            "auth.approver_identities 必须至少配置一个审批人。"
        )
