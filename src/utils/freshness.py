"""AI 工具结果的统一时效说明。"""
from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def epoch_to_iso(value: float | None) -> str:
    if value is None:
        return "未知"
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return "未知"


def freshness_note(
    *,
    source: str,
    observed_at: str | None = None,
    historical: bool = False,
    detail: str = "",
) -> str:
    """生成统一的数据时效说明，供所有 AI 工具结果追加。"""
    generated_at = utc_now_iso()
    observed = observed_at or generated_at
    lines = [
        "[数据时效]",
        f"来源：{source}",
        f"观测时间：{observed}",
        f"生成时间：{generated_at}",
    ]
    if historical:
        lines.append("性质：历史归档，不代表当前状态；需要当前状态时必须重新查询实时指标或告警。")
    else:
        lines.append("性质：查询时刻快照，之后可能发生变化；不能当作永久有效状态。")
    if detail:
        lines.append(f"说明：{detail}")
    return "\n".join(lines)
