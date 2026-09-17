"""公开的存活与就绪探针；只暴露状态，不返回地址或异常原文。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

router = APIRouter()


@router.get("/health", response_model=dict[str, Any])
def health(request: Request) -> dict[str, Any]:
    """无外部 I/O 的进程存活快照，始终返回 HTTP 200。"""
    return request.app.state.container.health_service().snapshot(probe=False)


@router.get("/ready", response_model=dict[str, Any])
def ready(request: Request, response: Response) -> dict[str, Any]:
    """主动探测 Prometheus；核心依赖未就绪时返回 HTTP 503。"""
    snapshot = request.app.state.container.health_service().snapshot(probe=True)
    if not snapshot["ready"]:
        response.status_code = 503
    return snapshot
