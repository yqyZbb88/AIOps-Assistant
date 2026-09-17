"""utils.security：敏感打码。"""
from __future__ import annotations

from src.utils.security import redact_sensitive, redact_text, register_secrets


def test_redact_masks_sensitive_keys_case_insensitive():
    data = {
        "password": "P@ssw0rd",
        "root_password": "secret1",
        "api_key": "abc123",
        "Token": "tok",
        "username": "root",
        "command": "pkill load_gen",
    }
    out = redact_sensitive(data)
    assert out["password"] == "***"
    assert out["root_password"] == "***"
    assert out["api_key"] == "***"
    assert out["Token"] == "***"
    # 非敏感字段原样保留
    assert out["username"] == "root"
    assert out["command"] == "pkill load_gen"
    # 不修改原 dict（副本语义，动作仍用原值执行）
    assert data["password"] == "P@ssw0rd"


def test_redact_returns_copy():
    data = {"a": 1}
    out = redact_sensitive(data)
    out["b"] = 2
    assert "b" not in data


def test_redact_recurses_without_mutating_input():
    data = {"outer": [{"password": "canary-password"}], "note": "token=canary-token"}
    out = redact_sensitive(data)
    assert out == {"outer": [{"password": "***"}], "note": "token=***"}
    assert data["outer"][0]["password"] == "canary-password"


def test_redact_text_masks_auth_url_private_key_and_registered_secret():
    register_secrets("exact-canary-secret")
    text = (
        "Authorization: Bearer abc123 https://u:pw@example.test/x?api_key=q "
        "-----BEGIN PRIVATE KEY----- payload -----END PRIVATE KEY----- exact-canary-secret"
    )
    cleaned = redact_text(text)
    for secret in ("abc123", "pw", "q", "payload", "exact-canary-secret"):
        assert secret not in cleaned
    assert cleaned.count("***") >= 5
