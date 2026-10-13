"""当前身份只来自服务端鉴权，不解析客户端 Token 内容。"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from src.api.deps import require_auth
from src.utils.auth import parse_approvers
from src.utils.host_access import is_admin

router = APIRouter()


@router.get("/identity")
def current_identity(request: Request, identity: str = Depends(require_auth)):
    auth = request.app.state.settings.auth
    roles = ["user"]
    if is_admin(request.app.state.settings, identity):
        roles.append("admin")
    if identity in parse_approvers(auth.approver_identities):
        roles.append("approver")
    if identity == auth.alert_identity:
        roles.append("webhook")
    return JSONResponse({"identity": identity, "roles": roles, "approval_mode": auth.approval_mode},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
