"""共享密钥鉴权的纯逻辑：配置解析、身份解析、启动期校验（全离线，不碰 FastAPI）。"""
from __future__ import annotations

import pytest

from src.utils.auth import (
    AuthConfigError,
    ensure_usable,
    parse_tokens,
    resolve_identity,
)

_TOKENS = '{"k-alice": "alice", "k-bob": "bob"}'


# -- 配置解析 ---------------------------------------------------------------
def test_unset_config_yields_no_tokens():
    """空串 / 纯空白 / None 一律当「没配」——鉴权是否生效另由 enabled 决定。"""
    assert parse_tokens("") == ()
    assert parse_tokens("   ") == ()
    # None 是 yaml 写 `tokens:` 不带值时的产物（_as_scalar 只处理「键缺失」的默认值）
    assert parse_tokens(None) == ()


def test_parses_mapping_preserving_order():
    assert parse_tokens(_TOKENS) == (("k-alice", "alice"), ("k-bob", "bob"))


def test_malformed_json_is_rejected():
    with pytest.raises(AuthConfigError, match="不是合法 JSON"):
        parse_tokens("{not json")


def test_non_mapping_is_rejected():
    with pytest.raises(AuthConfigError, match="必须是 JSON 对象"):
        parse_tokens('["alice"]')


@pytest.mark.parametrize(
    "raw",
    [
        '{"": "alice"}',      # 空密钥 → 传个空 Bearer 就能进
        '{"k-alice": ""}',    # 空身份 → 所有者校验失去意义
        '{"k-alice": 1}',     # 值不是字符串
        '{"k-alice": null}',
        '{"k-alice": ["a"]}',
    ],
)
def test_empty_or_nonstring_entries_are_rejected(raw):
    with pytest.raises(AuthConfigError):
        parse_tokens(raw)


# -- 身份解析 ---------------------------------------------------------------
def test_resolve_identity_matches_each_key():
    tokens = parse_tokens(_TOKENS)

    assert resolve_identity(tokens, "k-alice") == "alice"
    assert resolve_identity(tokens, "k-bob") == "bob"


def test_resolve_identity_rejects_unknown_and_empty():
    tokens = parse_tokens(_TOKENS)

    assert resolve_identity(tokens, "nope") is None
    assert resolve_identity(tokens, "") is None
    assert resolve_identity((), "k-alice") is None


def test_resolve_identity_handles_non_ascii():
    """`compare_digest` 收 str 时，任一侧含非 ASCII 会抛 TypeError → 500 而不是 401。

    所以实现里必须先 `.encode()`。这条把它钉死。
    """
    tokens = parse_tokens('{"密钥": "alice"}')

    assert resolve_identity(tokens, "密钥") == "alice"
    assert resolve_identity(tokens, "nope") is None
    # 反向：配置里是 ASCII、传进来是非 ASCII，同样不能炸
    assert resolve_identity(parse_tokens(_TOKENS), "密钥") is None


def test_resolve_identity_scans_every_token(monkeypatch):
    """**不提前返回**：命中第一个也要走完全部。

    提前 return 会让响应时间泄露「第几个密钥匹配」，也就泄露了猜的密钥前缀对不对。
    """
    import src.utils.auth as auth_mod

    calls: list[int] = []
    real = auth_mod.hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", counting)

    tokens = parse_tokens('{"k1": "alice", "k2": "bob", "k3": "carol"}')
    assert resolve_identity(tokens, "k1") == "alice"

    assert len(calls) == 3, "命中第一条也必须走完全部密钥"


# -- 启动期校验 -------------------------------------------------------------
def test_ensure_usable_is_noop_when_disabled():
    ensure_usable(False, "")
    ensure_usable(False, "{连 JSON 都不是")  # 关闭时连解析都不做


def test_ensure_usable_accepts_valid_config():
    ensure_usable(True, _TOKENS)


def test_ensure_usable_rejects_enabled_without_tokens():
    """绝不接受「空密钥 = 放行」——一次配置漏填就会让服务全开。"""
    with pytest.raises(AuthConfigError, match="解析不出任何密钥"):
        ensure_usable(True, "")


def test_ensure_usable_rejects_invalid_config():
    with pytest.raises(AuthConfigError):
        ensure_usable(True, "{not json")
