"""按稳定身份控制主机访问；普通身份无显式授权时默认拒绝。"""
from __future__ import annotations

import json
from functools import lru_cache

from src.utils.auth import AuthConfigError, parse_admins, parse_tokens


@lru_cache(maxsize=128)
def parse_host_access(raw: str) -> dict[str, tuple[str, ...]]:
    """只接受身份到主机 ID 数组的 JSON；不支持通配符或隐式全量权限。"""
    try:
        values = json.loads(raw)
        if not isinstance(values, dict):
            raise ValueError
        result = {}
        for identity, hosts in values.items():
            if (not isinstance(identity, str) or not identity.strip() or identity != identity.strip()
                    or len(identity) > 128 or any(ord(c) < 32 for c in identity)
                    or not isinstance(hosts, list)
                    or any(not isinstance(h, str) or not h or h != h.strip() or len(h) > 64
                           or h == "*" for h in hosts)):
                raise ValueError
            result[identity] = tuple(dict.fromkeys(hosts))
        return result
    except (ValueError, TypeError):
        raise AuthConfigError("auth.host_access 必须是身份到主机 ID 数组的 JSON 对象") from None


def is_admin(settings, identity: str | None) -> bool:
    return bool(identity and identity in parse_admins(settings.auth.admin_identities))


def may_access_host(settings, identity: str | None, host_id: str | None) -> bool:
    """还需有效身份映射；残留授权不能使已撤销身份继续审批或访问。"""
    if not identity or not host_id:
        return False
    if identity not in {value for _, value in parse_tokens(settings.auth.tokens)}:
        return False
    return is_admin(settings, identity) or host_id in parse_host_access(settings.auth.host_access).get(identity, ())
