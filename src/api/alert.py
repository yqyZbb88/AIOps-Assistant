"""告警路由：POST /alert 接收、GET /alerts/recent、GET /alerts/active。"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from src.core.agent import OpsAgent
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
    """查看最近接收的告警记录（内存缓冲，进程重启后清空）。"""
    return _agent(request).recent(limit)


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
