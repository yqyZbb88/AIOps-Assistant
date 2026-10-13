"""统一审计写入（aiops_assistant.audit）。

规则动作、Agent 写命令与只读诊断共用结构化通道：
- params 经敏感键、已注册值及文本模式脱敏，不保证识别未知的任意秘密；
- 调用方只传轻量结果与错误元信息，不把整段命令输出交给本函数。

结构 {action, command?, host, params(打码), started_at, finished_at, result|error}。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.utils.logger import write_audit
from src.utils.security import redact_sensitive


def now_iso() -> str:
    """当前 UTC 时间，ISO-8601 到秒（审计 started_at / finished_at 用）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_execution(
    *,
    action: str,
    host: str,
    params: dict[str, Any],
    started_at: str,
    command: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """写一条真实 SSH 尝试的结构化审计（成功记 info，失败记 error）。

    - action:   动作名 / 诊断通道名（如 restart_service、diagnostics）；
    - command:  命令标识（规则动作可为构建命令串，Agent/诊断为目录 key）；
    - params:   入参副本，内部做敏感打码，调用方可传原值；
    - result/error 二选一：result 只放轻量状态（不含整段输出）。
    """
    record: dict[str, Any] = {"action": action}
    if command is not None:
        record["command"] = command
    record["host"] = host
    record["params"] = redact_sensitive(dict(params))
    record["started_at"] = started_at
    record["finished_at"] = now_iso()

    if error is not None:
        record["error"] = redact_sensitive(error)
    else:
        record["result"] = redact_sensitive(result)
    write_audit(record, level=logging.ERROR if error is not None else logging.INFO)
