"""最高权限管理员的结构化配置接口。

本 router 的所有端点各自强制 `require_admin`，不依赖普通业务路由的鉴权挂载。
配置 GET 只返回可编辑脱敏草稿；
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


class AdminHostDelete(AdminConfigApply):
    model_config = {"extra": "forbid"}
    stop_collection: bool = False


class AdminTargetDelete(AdminConfigApply):
    model_config = {"extra": "forbid"}
    address: str = Field(..., min_length=1, max_length=256)


def _remove_target(request, payload, identity, host_id=None):
    try:
        return _onboarding(request).remove(payload.revision, host_id, identity,
            stop_collection=payload.stop_collection if host_id else True,
            address=payload.address if not host_id else "")
    except (AdminConfigConflict, RuntimeBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except AdminConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except OSError:
        raise HTTPException(status_code=500, detail="删除失败，已尝试恢复原配置，请刷新核对") from None


@router.delete("/hosts/{host_id}")
def delete_host(host_id: str, payload: AdminHostDelete, request: Request,
                identity: str = Depends(require_admin)):
    return _remove_target(request, payload, identity, host_id)


@router.delete("/targets")
def delete_managed_target(payload: AdminTargetDelete, request: Request,
                          identity: str = Depends(require_admin)):
    return _remove_target(request, payload, identity)


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
    # 票据属于唯一登记服务；首次并发请求也必须共用同一对象。
    with manager._lock:
        service = getattr(manager, "_host_onboarding", None)
        if service is None:
            from src.utils.host_onboarding import HostOnboarding
            service = HostOnboarding(manager)
            manager._host_onboarding = service
        return service


@router.get("/hosts/options", response_model=dict[str, Any])
def get_host_options(request: Request, discover: bool = True, _identity: str = Depends(require_admin)):
    try:
        # 选项仅含受限 ID/目标元信息，避免 credentials 键被通用密钥脱敏整个遮蔽。
        return _credential_response(_onboarding(request).options(discover=discover))
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


class CredentialUpdate(BaseModel):
    """密钥输入只用于本次写操作；校验错误不得回显输入内容。"""
    model_config = {"extra": "forbid"}
    revision: str = Field(..., min_length=64, max_length=64)
    data: dict[str, Any] = Field(default_factory=dict)


def _credential_service(request: Request):
    from src.utils.admin_credentials import AdminCredentials
    return AdminCredentials(_manager(request))


def _credential_response(result):
    # 此服务只返回经过显式投影的元信息。Token 创建/轮换是唯一一次性明文输出。
    # 不能走递归密钥遮蔽，否则 tokens 列表和 token_once 会一起被遮蔽。
    from fastapi.responses import JSONResponse
    from src.utils.security import redact_text
    def safe(value):
        if isinstance(value, dict):
            return {key: item if key == "token_once" else safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [safe(item) for item in value]
        return redact_text(value) if isinstance(value, str) else value
    return JSONResponse(safe(result), headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def _credential_change(request, payload, identity, kind, key, action):
    try:
        result = _credential_service(request).change(payload.revision, kind, key, action, payload.data, identity)
        return _credential_response(result)
    except (AdminConfigConflict, RuntimeBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except AdminConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except OSError:
        raise HTTPException(status_code=500, detail="凭据写入失败，未应用修改") from None


@router.get("/credentials")
def get_credentials(request: Request, identity: str = Depends(require_admin)):
    try:
        return _credential_response(_credential_service(request).snapshot())
    except AdminConfigUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.post("/credentials/tokens")
def create_user_token(payload: CredentialUpdate, request: Request, identity: str = Depends(require_admin)):
    return _credential_change(request, payload, identity, "tokens", "", "create")


@router.post("/credentials/tokens/{token_id}/rotate")
def rotate_user_token(token_id: str, payload: CredentialUpdate, request: Request, identity: str = Depends(require_admin)):
    return _credential_change(request, payload, identity, "tokens", token_id, "rotate")


@router.delete("/credentials/tokens/{token_id}")
def revoke_user_token(token_id: str, payload: CredentialUpdate, request: Request, identity: str = Depends(require_admin)):
    return _credential_change(request, payload, identity, "tokens", token_id, "delete")


@router.put("/credentials/ssh/{credential_id}")
def save_ssh_credential(credential_id: str, payload: CredentialUpdate, request: Request, identity: str = Depends(require_admin)):
    return _credential_change(request, payload, identity, "ssh", credential_id, "save")


@router.delete("/credentials/ssh/{credential_id}")
def delete_ssh_credential(credential_id: str, payload: CredentialUpdate, request: Request, identity: str = Depends(require_admin)):
    return _credential_change(request, payload, identity, "ssh", credential_id, "delete")


@router.put("/credentials/model")
def save_model_configuration(payload: CredentialUpdate, request: Request, identity: str = Depends(require_admin)):
    return _credential_change(request, payload, identity, "model", "", "save")


@router.post("/credentials/model/test")
def test_model_configuration(request: Request, identity: str = Depends(require_admin)):
    from src.utils.model_loader import get_model, content_text
    from src.utils.admin_credentials import CredentialStore
    manager = _manager(request)
    config = manager.runtime.current.container._model_config
    if config is None:
        config = CredentialStore.model_config(manager.credential_store.read())
    if not config.get("api_key"):
        raise HTTPException(status_code=422, detail="尚未配置模型密钥")
    try:
        # 仅显式点击触发，限制为一次最短请求；不返回模型正文或连接异常原文。
        response = get_model({**config, "timeout": 10, "max_retries": 0, "max_tokens": 16}).invoke("Reply with OK.")
        if not content_text(response.content).strip():
            raise ValueError
    except Exception:
        raise HTTPException(status_code=502, detail="模型连接测试失败，请检查地址、密钥、模型名和服务状态") from None
    return {"ok": True, "detail": "模型连接正常"}


@router.put("/credentials/hosts/{host_id}/ssh")
def bind_host_credential(host_id: str, payload: CredentialUpdate, request: Request, identity: str = Depends(require_admin)):
    return _credential_change(request, payload, identity, "host", host_id, "save")


@router.post("/credentials/hosts/{host_id}/ssh/test")
def test_registered_ssh(host_id: str, request: Request, identity: str = Depends(require_admin)):
    try:
        return _onboarding(request).check_registered_ssh(host_id)
    except AdminConfigError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
