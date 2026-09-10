"""utils.security：敏感打码。"""
from __future__ import annotations

from src.utils.security import redact_sensitive


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


def test_redact_is_shallow_copy():
    data = {"a": 1}
    out = redact_sensitive(data)
    out["b"] = 2
    assert "b" not in data
