"""最高权限管理员的结构化配置接口。

本 router 不挂普通 `require_auth`：所有端点各自强制 `require_admin`，因此即使
`auth.enabled=false` 也不会匿名开放。配置 GET 只返回可编辑脱敏草稿；
PUT 校验、备份、写回后切换运行快照；主机向导只接受已发现或自管目标。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.api.deps import require_admin
from src.utils.admin_config import (
    AdminConfigConflict,
    AdminConfigError,
    AdminConfigUnavailable,
)
from src.utils.runtime import RuntimeBusyError

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminConfigUpdate(BaseModel):
    """整份草稿写回请求；revision 必须来自最近一次 GET 的磁盘版本。"""
    revision: str = Field(..., min_length=64, max_length=64)
    config: dict[str, Any]


class AdminConfigApply(BaseModel):
    revision: str = Field(..., min_length=64, max_length=64)


class AdminHostRequest(BaseModel):
    revision: str = Field(..., min_length=64, max_length=64)
    draft: dict[str, Any]


def _manager(request: Request):
    manager = getattr(request.app.state, "admin_config", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="管理员配置服务不可用")
    return manager


@router.get("/config", response_model=dict[str, Any])
def get_admin_config(
    request: Request,
    _identity: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _manager(request).snapshot()
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (AdminConfigError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=f"磁盘配置无效: {exc}") from exc


@router.put("/config", response_model=dict[str, Any])
def put_admin_config(
    payload: AdminConfigUpdate,
    request: Request,
    identity: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _manager(request).update(
            expected_revision=payload.revision,
            config=payload.config,
            actor=identity,
        )
    except AdminConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AdminConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"配置写入失败，已尝试回滚: {exc}") from exc


@router.post("/config/apply", response_model=dict[str, Any])
def apply_admin_config(
    payload: AdminConfigApply,
    request: Request,
    identity: str = Depends(require_admin),
) -> dict[str, Any]:
    try:
        return _manager(request).apply_disk(
            expected_revision=payload.revision, actor=identity
        )
    except (AdminConfigConflict, RuntimeBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AdminConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _onboarding(request: Request):
    manager = _manager(request)
    service = getattr(manager, "_host_onboarding", None)
    if service is None:
        from src.utils.host_onboarding import HostOnboarding
        service = HostOnboarding(manager)
        manager._host_onboarding = service
    return service


@router.get("/hosts/options", response_model=dict[str, Any])
def get_host_options(request: Request, _identity: str = Depends(require_admin)):
    try:
        return _onboarding(request).options()
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/hosts/preview", response_model=dict[str, Any])
def preview_host(payload: AdminHostRequest, request: Request,
                 _identity: str = Depends(require_admin)):
    try:
        return _onboarding(request).preview(payload.revision, payload.draft)
    except AdminConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AdminConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/hosts/ssh-check", response_model=dict[str, Any])
def check_host_ssh(payload: AdminHostRequest, request: Request,
                   _identity: str = Depends(require_admin)):
    try:
        return _onboarding(request).ssh_check(payload.revision, payload.draft)
    except AdminConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AdminConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/hosts/add", response_model=dict[str, Any])
def add_host(payload: AdminHostRequest, request: Request,
             identity: str = Depends(require_admin)):
    try:
        return _onboarding(request).add(payload.revision, payload.draft, identity)
    except (AdminConfigConflict, RuntimeBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AdminConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail="主机保存失败，已尝试回滚") from exc
