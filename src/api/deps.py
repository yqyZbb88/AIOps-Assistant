"""共享的 FastAPI 依赖：共享密钥鉴权。

**身份由密钥推断**（服务端事实），不再从请求体里读客户端自报的 `user` —— 这是
「不同用户不能进同一段对话」得以成立的前提：自报的用户名无从核实，基于它的校验
只是装饰。

挂载方式见 `src/main.py`：`include_router(..., dependencies=[Depends(require_auth)])`。
这种写法是 **fail-closed** 的——要豁免某组路由必须显式写出来（目前只有 /health）。

两条纪律：
- **不在 import 期读配置文件**，设置一律从 `request.app.state.settings` 取
  （main.create_app 已就位）。import 期读盘会破坏无依赖降级部署的语义
  （见 tests/test_no_langchain_import.py）。
- **不 import 重量依赖**（langchain / langgraph / paramiko / dotenv）。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.utils.auth import AuthConfigError, parse_tokens, resolve_identity

# auto_error=False：缺 Authorization 头时**不由 FastAPI 直接拒绝**。
# auto_error=True 会返回 403，而「没带凭据」语义上是 401（FastAPI 的已知行为）。
# 顺带一个好处：/docs 里会出现标准的 Authorize 按钮。
_bearer = HTTPBearer(auto_error=False, description="形如 `Bearer <密钥>`")


def require_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str | None:
    """校验 Bearer 密钥并返回身份；鉴权未启用时返回 `None`。

    返回值的两种含义（`None` 是「**无身份**」，不是「认证失败」）：

    - 鉴权关闭 → `None`：调用方据此**跳过**所有者校验，行为与改造前完全一致；
    - 鉴权开启 → 配置里映射的身份字符串，是**服务端事实**，调用方直接采信。

    注意 `credentials` 为 `None` 覆盖了两种情况：没带 Authorization 头、或带了但
    scheme 不是 Bearer —— `HTTPBearer(auto_error=False)` 两种都回 `None`。
    """
    # 防御式取用：照 health.py 的先例。裸 FastAPI 实例（测试里可能有）不该 500。
    settings = getattr(request.app.state, "settings", None)
    auth = getattr(settings, "auth", None)
    if auth is None or not auth.enabled:
        return None

    try:
        tokens = parse_tokens(auth.tokens)
    except AuthConfigError as exc:
        # 启动期已由 main.create_app 的 ensure_usable 拦过一道，这里只是安全网：
        # 万一有人绕过 create_app 直接挂路由，也不能退化成「放行」。
        raise HTTPException(status_code=500, detail=f"鉴权配置无效: {exc}") from exc
    if not tokens:
        raise HTTPException(status_code=500, detail="auth.enabled 为真但未配置任何密钥")

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

    三种放行/拒绝：

    - 鉴权关闭（`identity is None`）→ 放行（也谈不上身份，行为与改造前一致）；
    - `alert_identity` **留空** → 放行（有意的 fail-open，保持既有部署不变；
      启动时会有 WARNING 提醒）；
    - 否则身份必须**精确匹配**，不匹配 → **403**。

    **为什么这里用 403 而会话那边用 404**：那边要藏「这个 id 存不存在」，
    而这里调用者已经通过鉴权了，只是没权限——「认证过但被拒」在语义上就是 403。
    """
    auth = getattr(getattr(request.app.state, "settings", None), "auth", None)
    allowed = (getattr(auth, "alert_identity", "") or "").strip()
    if identity is None or not allowed or identity == allowed:
        return identity
    raise HTTPException(
        status_code=403,
        detail=f"该身份无权调用此接口（需要 {allowed}）",
    )
