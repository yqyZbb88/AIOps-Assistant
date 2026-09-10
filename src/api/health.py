"""健康检查路由。"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health", response_model=dict[str, str])
def health() -> dict[str, str]:
    """健康检查接口，便于接入监控 / 负载均衡。"""
    return {"status": "ok"}
