"""共享的 FastAPI 依赖：共享密钥鉴权。

**身份由服务端 Token 映射核验**，不从 Token 内容或请求体中的 `user` 推断——这是
「不同用户不能进同一段对话」得以成立的前提：自报的用户名无从核实，基于它的校验
只是装饰。

挂载方式见 `src/main.py`：`include_router(..., dependencies=[Depends(require_auth)])`。
这种写法让普通业务 router 显式挂鉴权；`/health`、`/ready` 与 `/console`
公开但不返回业务数据。管理员路由单独使用 `require_admin`；业务鉴权没有关闭开关。
`/docs`、`/redoc`、`/openapi.json` 属于 FastAPI 应用级路由，不经过这些 router 依赖。

两条纪律：
- **不在 import 期读配置文件**，设置一律从 `request.app.state.settings` 取
  （main.create_app 已就位）。import 期读盘会破坏无依赖降级部署的语义
  （见 tests/test_no_langchain_import.py）。
- **不 import 重量依赖**（langchain / langgraph / paramiko / dotenv）。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.utils.auth import AuthConfigError, parse_admins, parse_tokens, resolve_identity

# 由 require_auth 统一处理缺失/无效凭据的 401，避免依赖 SDK 默认异常文案。
# 使用 HTTPBearer 同时为 OpenAPI 提供标准的 Authorize 入口。
_bearer = HTTPBearer(auto_error=False, description="形如 `Bearer <密钥>`")


def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """业务接口始终校验 Token，并按身份执行归属和权限校验。"""
    settings = getattr(request.app.state, "settings", None)
    auth = getattr(settings, "auth", None)
    if auth is None:
        raise HTTPException(status_code=503, detail="身份配置不可用")

    try:
        tokens = parse_tokens(auth.tokens)
    except AuthConfigError as exc:
        # 启动期已由 main.create_app 的 ensure_usable 拦过一道，这里只是安全网：
        # 万一有人绕过 create_app 直接挂路由，也不能退化成「放行」。
        raise HTTPException(status_code=500, detail=f"鉴权配置无效: {exc}") from exc
    if not tokens:
        raise HTTPException(status_code=503, detail="未配置业务身份，请恢复部署凭据")

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="缺少 Bearer 凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    identity = resolve_identity(tokens, credentials.credentials)
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail="凭据无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.identity = identity
    return identity


def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    """始终要求管理员 Token。"""
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="缺少 Bearer 凭据",
            headers={"WWW-Authenticate": "Bearer"},
        )
    auth = getattr(getattr(request.app.state, "settings", None), "auth", None)
    try:
        tokens = parse_tokens(getattr(auth, "tokens", ""))
        admins = parse_admins(getattr(auth, "admin_identities", "[]"))
    except AuthConfigError as exc:
        raise HTTPException(status_code=500, detail=f"管理员鉴权配置无效: {exc}") from exc
    identity = resolve_identity(tokens, credentials.credentials)
    if identity is None:
        raise HTTPException(
            status_code=401,
            detail="凭据无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if identity not in admins:
        raise HTTPException(status_code=403, detail="该身份没有系统管理权限")
    return identity


def require_alert_identity(
    request: Request,
    identity: str | None = Depends(require_auth),
) -> str | None:
    """在「密钥有效」之上再加一层**授权**：`/alert` 只认 `auth.alert_identity`。

    因为 `POST /alert` 不是只读接口——它把告警喂给 `RuleEngine`，而
    `auto_execute: true` 的规则会**直接执行写命令、不经人工审批**。不设限的话，
    任何持有效密钥的聊天用户都能构造告警去踩那条路径，等于从「聊天用户」越权到
    「可触发写命令」。

    已通过身份认证后的附加规则：

    - `alert_identity` **留空** → 放行（有意的 fail-open，保持既有部署不变；
      启动时会有 WARNING 提醒）；
    - 否则身份必须**精确匹配**，不匹配 → **403**。

    **为什么这里用 403 而会话那边用 404**：那边要藏「这个 id 存不存在」，
    而这里调用者已经通过鉴权了，只是没权限——「认证过但被拒」在语义上就是 403。
    """
    auth = getattr(getattr(request.app.state, "settings", None), "auth", None)
    allowed = (getattr(auth, "alert_identity", "") or "").strip()
    if not allowed or identity == allowed:
        return identity
    raise HTTPException(
        status_code=403,
        detail=f"该身份无权调用此接口（需要 {allowed}）",
    )
