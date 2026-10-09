"""业务路由共用的主机授权；依赖 require_auth 先设置可信请求身份。"""
from __future__ import annotations

from fastapi import HTTPException, Request

from src.interfaces.host import HostError
from src.utils.host_access import is_admin, may_access_host


def identity(request: Request) -> str:
    value = getattr(request.state, "identity", None)
    if not value:
        raise HTTPException(status_code=401, detail="缺少有效身份", headers={"WWW-Authenticate": "Bearer"})
    return value


def can_access(request: Request, host_id: str | None, *, subject: str | None = None) -> bool:
    return bool(host_id and request.app.state.container.hosts().get(host_id)
                and may_access_host(request.app.state.settings, identity(request) if subject is None else subject, host_id))


def visible_host_ids(request: Request, *, include_disabled: bool = True) -> set[str]:
    return {h.host_id for h in request.app.state.container.hosts().all(include_disabled=include_disabled)
            if can_access(request, h.host_id)}


def require_host(request: Request, host_id: str, *, enabled: bool = True):
    registry = request.app.state.container.hosts()
    # 不区分未登记、未授权和已禁用，避免泄露主机存在性及配置。
    if not can_access(request, host_id):
        raise HTTPException(status_code=404, detail="主机不存在或不可访问")
    try:
        return registry.require(host_id, enabled=enabled)
    except HostError:
        raise HTTPException(status_code=404, detail="主机不存在或不可访问") from None


def recent_records(request: Request, fetch, *, limit: int, host_id: str | None, timestamp: str):
    """先按主机查询再合并，避免未授权的新记录挤掉较旧的可见记录。"""
    if host_id is not None:
        require_host(request, host_id)
        return list(fetch(limit=limit, host_id=host_id))
    if is_admin(request.app.state.settings, identity(request)):
        return list(fetch(limit=limit, host_id=None))
    records = []
    for current in sorted(visible_host_ids(request)):
        records.extend(fetch(limit=limit, host_id=current))
    def sort_key(item):
        row = item if isinstance(item, dict) else item.to_dict()
        return str(row.get(timestamp) or "")
    return sorted(records, key=sort_key, reverse=True)[:limit]
