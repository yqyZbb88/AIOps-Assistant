"""统一审计写入（ops_agent.audit）。

收敛动作（原 SshActionBase._audit）与只读诊断（原 SshDiagnostics._record）里近乎重复的
JSON 拼装，两条边界共用同一通道与结构。安全不变量：
- params 一律过 redact_sensitive（password/token/secret/api_key → ***），绝不落密码；
- 整段命令输出不落日志；error/result 只记状态与字符信息。

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
    - command:  命令标识（写命令 = 实际构建的命令串；诊断 = 命令 key）；
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
        record["error"] = error
    else:
        record["result"] = result
    write_audit(record, level=logging.ERROR if error is not None else logging.INFO)
