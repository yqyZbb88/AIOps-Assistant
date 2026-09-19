"""受保护的静态主机清单 API。"""
from __future__ import annotations

from typing import Any

import re

from fastapi import APIRouter, HTTPException, Query, Request

from src.interfaces.host import HostError
from src.interfaces.monitor import MonitorError

router = APIRouter()


@router.get("/hosts", response_model=list[dict[str, Any]])
def list_hosts(
    request: Request,
    include_disabled: bool = Query(True),
) -> list[dict[str, Any]]:
    registry = request.app.state.container.hosts()
    return [
        host.to_public_dict()
        for host in registry.all(include_disabled=include_disabled)
    ]


@router.get("/executions", response_model=dict[str, Any])
def list_executions(
    request: Request,
    host_id: str | None = Query(None, description="按主机 ID 筛选"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    if host_id is not None:
        _enabled_host(request, host_id)
    store = request.app.state.container.execution_store()
    if store is None:
        return {"storage": "off", "executions": []}
    return {
        "storage": "ok",
        "executions": [
            item.to_dict() for item in store.recent(limit=limit, host_id=host_id)
        ],
    }


@router.get("/hosts/{host_id}", response_model=dict[str, Any])
def get_host(host_id: str, request: Request) -> dict[str, Any]:
    host = request.app.state.container.hosts().get(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail=f"未知主机 ID: {host_id}")
    return host.to_public_dict()


def _enabled_host(request: Request, host_id: str):
    try:
        return request.app.state.container.hosts().require(host_id)
    except HostError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/hosts/{host_id}/status", response_model=dict[str, Any])
def get_host_status(host_id: str, request: Request) -> dict[str, Any]:
    host = _enabled_host(request, host_id)
    try:
        return request.app.state.container.host_status_service().status(host)
    except MonitorError as exc:
        return {
            "host": host.to_public_dict(),
            "exporter": {"state": "query_error", "value": None},
            "metrics": {},
            "active_alerts": [],
        }


@router.get("/hosts/{host_id}/metrics/{metric_id}", response_model=dict[str, Any])
def get_host_metric(host_id: str, metric_id: str, request: Request) -> dict[str, Any]:
    host = _enabled_host(request, host_id)
    try:
        samples = request.app.state.container.monitor().query_metric(
            metric_id, target=host.monitor
        )
    except MonitorError as exc:
        raise HTTPException(status_code=502, detail=f"监控源查询失败: {exc}") from exc
    return {
        "host_id": host.host_id,
        "metric_id": metric_id,
        "state": "ok" if samples else "no_data",
        "samples": [
            {
                "labels": dict(sample.labels),
                "value": sample.value,
                "timestamp": sample.timestamp,
            }
            for sample in samples
        ],
    }


@router.get(
    "/hosts/{host_id}/metrics/{metric_id}/trend",
    response_model=dict[str, Any],
)
def get_host_metric_trend(
    host_id: str,
    metric_id: str,
    request: Request,
    minutes: int = Query(60, ge=1, le=1440),
    step: str = Query("5m", min_length=2, max_length=8),
) -> dict[str, Any]:
    if re.fullmatch(r"[1-9][0-9]*[smhd]", step) is None:
        raise HTTPException(status_code=422, detail="step 必须是正整数加 s/m/h/d")
    host = _enabled_host(request, host_id)
    try:
        trend = request.app.state.container.monitor().query_metric_trend(
            metric_id,
            target=host.monitor,
            minutes=minutes,
            step=step,
        )
    except MonitorError as exc:
        raise HTTPException(status_code=502, detail=f"监控源查询失败: {exc}") from exc
    return {
        "host_id": host.host_id,
        "metric_id": metric_id,
        "state": "ok" if trend.get("points") else "no_data",
        "trend": trend,
    }
