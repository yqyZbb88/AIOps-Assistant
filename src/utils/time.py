"""统一时间戳格式。

项目统一使用秒级 UTC ISO-8601 时间戳。TTL 清理依赖字符串比较，时间格式必须完全一致，
否则过期判断可能静默失效。新代码应统一调用本模块。
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_iso() -> str:
    """当前 UTC 时间戳，秒级精度（与既有代码逐字一致的格式）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(epoch_seconds: float) -> str:
    """把 epoch 秒转成与 now_iso() 同格式的时间戳。"""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat(timespec="seconds")


def cutoff_iso(now_seconds: float, ttl_seconds: float) -> str:
    """返回 `now - ttl` 的时间戳，供 `WHERE created_at < cutoff` 做字符串比较。"""
    return to_iso(now_seconds - ttl_seconds)


__all__ = ["now_iso", "to_iso", "cutoff_iso"]
