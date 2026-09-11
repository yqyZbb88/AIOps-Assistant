"""统一时间戳格式（全项目只此一处）。

为什么单独成模块：`datetime.now(timezone.utc).isoformat(timespec="seconds")` 这个组合
原先在 6 个文件里各写了一遍（assistant / approval / audit / rule_engine / memory /
analysis）。而**TTL 清理靠字符串比较**（`WHERE created_at < ?`）——ISO-8601 在同样带
UTC 偏移时字典序等于时间序，所以只有格式**逐字一致**时比较才成立。

一旦某处写成别的精度或本地时区，清理会**静默失效**：比较恒为「未过期」，不报任何错，
表现为「过期数据一直不删」。新代码一律用本模块，别再写第 7 份。
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
