"""FastAPI 应用入口：装配依赖容器、生命周期与 HTTP 路由。

启动方式（仓库根执行）：
    uvicorn src.main:app --reload --host 127.0.0.1 --port 8000     （推荐，热重载）
    python src/main.py                                              （直跑分支）

鉴权（config/settings.yaml 的 auth 段）：
    启用后所有含运维数据或写操作的 API 都要求 `Authorization: Bearer <密钥>`；
    /health、/ready 和无数据的 /status 静态壳公开。
    FastAPI 自带的 /docs、/redoc、/openapi.json 是应用级文档路由，当前公开。
    身份由密钥推断（服务端事实），请求体不能自报用户身份。
    `auth.enabled: true` 却没有任何可用密钥时会**拒绝启动**，绝不静默地无鉴权运行。

对外路由：
    GET  /health            进程存活与缓存健康状态
    GET  /ready             主动依赖就绪检查
    POST /chat              使用既有 session_id 进行聊天
    POST /alert             Alertmanager webhook
    GET  /alerts/recent     最近接收的告警
    GET  /alerts/active     实时活动告警（监控源查询失败转 502）
    GET  /alerts/history    告警、处置和 AI 建议归档
    GET  /inspections/recent 最近异常固定指标巡检归档

人工审批（Agent 写命令，config/agent_actions.yaml 启用后才有内容）：
    GET  /approvals             待审批的写命令调用
    POST /approvals/{id}        提交 approve/reject 并续跑

对话历史（config/settings.yaml 的 storage 段启用后才有内容）：
    GET    /sessions            会话列表（最近更新的在前）
    GET    /sessions/{id}       会话详情（含全部消息）
    PATCH  /sessions/{id}       修改会话标签
    DELETE /sessions/{id}       删除会话、消息和关联审批决策，并取消未决审批
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError

# 兼容直接运行（python src/main.py）：__package__ 为空时把仓库根加入 sys.path
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import alert as alert_api
from src.api import approvals as approvals_api
from src.api import chat as chat_api
from src.api import health as health_api
from src.api import hosts as hosts_api
from src.api import inspections as inspections_api
from src.api import sessions as sessions_api
from src.api import status as status_api
from src.api.deps import require_auth
from src.api.response import RedactingJSONResponse
from src.container import Container
from src.utils.auth import AuthConfigError, ensure_usable
from src.utils.config_loader import Settings
from src.utils.logger import configure_logging
from src.utils.security import configure_secrets

logger = logging.getLogger("aiops_assistant.main")

_DESCRIPTION = (
    "智能运维 Agent：关键词/AI 聊天 + Prometheus 指标查询 + 告警状态主动查询"
    " + Alertmanager 告警接收 + 规则自动处置 + 固定指标自动巡检"
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """管理巡检后台线程；仅在 inspection.enabled=true 时启动。"""
    scheduler = None
    if app.state.settings.inspection.enabled:
        scheduler = app.state.container.inspection_scheduler()
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配依赖容器并构建 FastAPI 应用（app.state.agent 供路由依赖注入取用）。"""
    container = Container(settings=settings)
    settings_ = container.settings
    configure_secrets(auth_tokens=settings_.auth.tokens)
    configure_logging(
        level=settings_.logging.level,
        audit=settings_.logging.audit,
        audit_file=settings_.logging.audit_file,
        audit_max_bytes=settings_.logging.audit_max_bytes,
        audit_backup_count=settings_.logging.audit_backup_count,
        inspection_file=settings_.logging.inspection_file,
        inspection_max_bytes=settings_.logging.inspection_max_bytes,
        inspection_backup_count=settings_.logging.inspection_backup_count,
    )
    agent = container.aiops_assistant()
    _check_auth(settings_)

    app = FastAPI(
        title=settings_.app.name,
        description=_DESCRIPTION,
        version=settings_.app.version,
        lifespan=_lifespan,
        default_response_class=RedactingJSONResponse,
    )

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException):
        return RedactingJSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError):
        return RedactingJSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors())},
        )
    app.state.container = container
    app.state.settings = settings_
    app.state.agent = agent

    # 鉴权在这里挂：include_router 是本仓库唯一的挂载点，一屏看完，
    # 且形状是 **fail-closed** —— 新增业务 router 时必须在这里显式决定是否挂鉴权。
    #
    # 不用中间件：中间件在路由匹配**之前**跑，拿不到「这条路由要不要鉴权」，只能手工
    # 维护路径白名单，而白名单漏掉一个新路由 = 新路由默认敞开（最坏的失败方向）。
    # 也不用各模块单独声明 router 级依赖：集中挂载更容易审查完整业务接口面。
    #
    # /docs、/redoc、/openapi.json 是 app 级路由，不在任何 router 里，不会被拦。
    app.include_router(health_api.router)  # 探针豁免：健康检查不该要密钥
    app.include_router(status_api.router)  # 无数据静态壳；真实数据 API 仍要求 Bearer
    app.include_router(chat_api.router, dependencies=[Depends(require_auth)])
    app.include_router(alert_api.router, dependencies=[Depends(require_auth)])
    app.include_router(approvals_api.router, dependencies=[Depends(require_auth)])
    app.include_router(sessions_api.router, dependencies=[Depends(require_auth)])
    app.include_router(inspections_api.router, dependencies=[Depends(require_auth)])
    app.include_router(hosts_api.router, dependencies=[Depends(require_auth)])
    return app


def _check_auth(settings: Settings) -> None:
    """鉴权配置的启动期校验：**宁可拒绝启动，也不静默地无鉴权运行**。

    - `enabled: true` 却解析不出任何密钥 → 抛异常。绝不接受「空密钥 = 放行」：
      一次配置漏填就会让服务全开，而失败方向恰好是最坏的那一侧。
    - 反向情况（配了密钥但 `enabled: false`）→ 只 WARNING。这不影响安全性，
      但用户很可能**以为自己受保护了**，值得在启动日志里说一句。
    """
    try:
        ensure_usable(
            settings.auth.enabled,
            settings.auth.tokens,
            approval_mode=settings.auth.approval_mode,
            approvers_raw=settings.auth.approver_identities,
        )
    except AuthConfigError as exc:
        raise RuntimeError(f"鉴权配置有误，拒绝启动：{exc}") from exc

    if not settings.auth.enabled and (settings.auth.tokens or "").strip():
        logger.warning(
            "已提供 OPS_API_TOKENS，但 auth.enabled 为 false —— "
            "服务仍在**无鉴权**运行，所有接口对能访问该端口的人开放。"
        )

    if settings.auth.enabled and not (settings.auth.alert_identity or "").strip():
        logger.warning(
            "auth.alert_identity 为空 —— POST /alert 对**任何**有效密钥开放。"
            "而它会喂给规则引擎（auto_execute: true 的规则直接执行写命令、不经审批），"
            "等于每个聊天用户都能触发写入。建议在 settings.yaml 里设成 webhook 专用身份。"
        )


# 模块级实例：uvicorn src.main:app 直接引用的对象
app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = app.state.settings
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
