"""告警路由：POST /alert 接收、GET /alerts/recent、GET /alerts/active、GET /alerts/history。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from src.core.agent import OpsAgent
from src.interfaces.history import HistoryError
from src.interfaces.monitor import MonitorError
from src.storage.models import AlertAck, AlertmanagerPayload

logger = logging.getLogger("ops_agent.api.alert")

router = APIRouter()


def _agent(request: Request) -> OpsAgent:
    return request.app.state.agent


@router.post("/alert", response_model=AlertAck)
def alert(payload: AlertmanagerPayload, request: Request) -> AlertAck:
    """接收 Alertmanager webhook 推送的告警。

    处理由 OpsAgent 编排：记录 → 规则处置（auto_execute=true 才动作）→
    无规则告警 AI 分析（失败不阻塞）。
    """
    logger.info(
        "收到 Alertmanager 通知: %d 条, 整组状态=%s, receiver=%s",
        len(payload.alerts), payload.status, payload.receiver,
    )
    return _agent(request).process_alert(payload)


@router.get("/alerts/recent", response_model=list[dict[str, Any]])
def alerts_recent(request: Request, limit: int = 20) -> list[dict[str, Any]]:
    """最近接收的告警记录（**内存热缓冲**，进程重启后清空）。

    与 /alerts/history 的区别：这个是「刚发生了什么」的快速视图（有界 deque，
    不落盘、容量小）；history 是落盘归档（可跨重启查、可关联处置与 AI 建议）。
    两者数据源不同，**不是一个东西的两种读法**。
    """
    return _agent(request).recent(limit)


@router.get("/alerts/history", response_model=dict[str, Any])
def alerts_history(
    request: Request,
    limit: int = Query(50, ge=1, le=500, description="每类最多返回条数"),
) -> dict[str, Any]:
    """落盘的告警归档：告警 + 处置结果 + AI 建议，各取最近 limit 条。

    持久化未启用时三者均为**空列表**（沿用「能力缺失 = 空而非报错」的约定）；
    存储故障则 503——那是故障，不该伪装成「没有记录」。
    """
    store = request.app.state.container.history()
    if store is None:
        return {"alerts": [], "remediations": [], "advice": [], "storage": "off"}
    try:
        return {
            "alerts": [r.to_dict() for r in store.recent_alerts(limit=limit)],
            "remediations": [r.to_dict() for r in store.recent_remediations(limit=limit)],
            "advice": [r.to_dict() for r in store.recent_advice(limit=limit)],
            "storage": "ok" if store.healthy else "degraded",
        }
    except HistoryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/alerts/active", response_model=list[dict[str, Any]])
def alerts_active(request: Request) -> list[dict[str, Any]]:
    """主动查询监控源当前 pending/firing 告警（实时，非本地缓冲）。

    与 /alerts/recent 的区别：recent 是本服务被动收到的记录；
    active 是实时向监控源 /api/v1/alerts 要当前规则状态。
    """
    try:
        return _agent(request).active_alerts()
    except MonitorError as exc:
        logger.warning("查询监控源告警状态失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"监控源查询失败: {exc}") from exc
