"""巡检查询路由：只读最近异常巡检归档。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from src.interfaces.inspection import InspectionError
from src.interfaces.host import HostError

router = APIRouter()


@router.get("/inspections/recent", response_model=dict[str, Any])
def inspections_recent(
    request: Request,
    limit: int = Query(50, ge=1, le=500, description="最多返回条数"),
    host_id: str | None = Query(None, description="按主机 ID 筛选"),
) -> dict[str, Any]:
    """最近异常巡检记录；normal 轮次按设计不落库。"""
    store = request.app.state.container.inspection_store()
    if store is None:
        return {"storage": "off", "inspections": []}
    try:
        if host_id is not None:
            request.app.state.container.hosts().require(host_id)
        records = store.recent(limit=limit, host_id=host_id)
    except HostError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InspectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "storage": "ok" if store.healthy else "degraded",
        "inspections": [record.to_dict() for record in records],
    }
