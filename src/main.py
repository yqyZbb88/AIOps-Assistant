"""FastAPI 入口：装配依赖、请求快照、路由和唯一巡检调度器。

业务 API 强制身份校验，管理 API 另校验管理员。控制台空壳、指定资源、探针及
OpenAPI 文档公开。容器通过 scripts.deployment 装配环境后以单 worker 启动。
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError

from src.api import admin as admin_api
from src.api import alert as alert_api
from src.api import approvals as approvals_api
from src.api import chat as chat_api
from src.api import health as health_api
from src.api import hosts as hosts_api
from src.api import identity as identity_api
from src.api import inspections as inspections_api
from src.api import sessions as sessions_api
from src.api import status as status_api
from src.api.deps import require_auth
from src.api.response import RedactingJSONResponse
from src.container import Container
from src.utils.auth import AuthConfigError, ensure_usable, parse_admins, parse_tokens
from src.utils.host_access import parse_host_access
from src.utils.admin_config import AdminConfigManager
from src.utils.config_loader import Settings, load_settings
from src.utils.logger import configure_logging
from src.utils.security import configure_secrets
from src.utils.runtime import RuntimeManager, RuntimeProxy, RuntimeSnapshot

logger = logging.getLogger("aiops_assistant.main")

_DESCRIPTION = (
    "智能运维 Agent：关键词/AI 聊天 + Prometheus 指标查询 + 告警状态主动查询"
    " + Alertmanager 告警接收 + 规则自动处置 + 固定指标自动巡检"
)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """管理唯一巡检线程；启停开关由每轮的运行快照决定。"""
    runtime = app.state.runtime
    scheduler = runtime.current.container.inspection_scheduler()
    scheduler.configure_runtime(
        lambda: (
            runtime.current.container.inspection_checker(),
            runtime.current.settings.inspection,
        )
    )
    runtime.scheduler = scheduler
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()


def create_app(
    settings: Settings | None = None,
    *,
    admin_config_manager: AdminConfigManager | None = None,
) -> FastAPI:
    """构建应用与初始运行快照；文件启动时校验身份、主机 ACL 和目录引用。

    settings 与管理器可显式注入用于隔离测试；未传入时读取运行配置。
    """
    settings_ = settings or load_settings()
    manager = admin_config_manager or AdminConfigManager(settings_)
    # 真实启动及显式注入的管理配置在创建第一份快照时也冻结 YAML。
    # 否则 Agent 等惰性组件可能在管理员应用前读到页面外的磁盘改动。
    config_texts = None
    if settings is None or admin_config_manager is not None:
        initial_revision = manager.active_revision
        config_texts = {
            name: path.read_bytes().decode("utf-8")
            for name, path in manager.paths.items()
        }
        if manager.revision() != initial_revision:
            raise RuntimeError("启动期间配置文件发生变化，请重新启动")
    credential_state = manager.credential_store.read()
    settings_ = manager.credential_store.settings(settings_, credential_state)
    container = Container(settings=settings_, config_texts=config_texts,
                          credential_specs=manager.credential_store.ssh_specs(credential_state),
                          model_config=manager.credential_store.model_config(credential_state) if credential_state["model"] is not None else None)
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
    _check_auth(settings_, require_tokens=settings is None or admin_config_manager is not None)
    known_hosts = {host.host_id for host in container.hosts().all()}
    if any(set(ids) - known_hosts for ids in parse_host_access(settings_.auth.host_access).values()):
        raise RuntimeError("用户主机授权包含未登记主机，拒绝启动")

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
        if _request.url.path.startswith("/admin/credentials"):
            return RedactingJSONResponse(status_code=422, content={"detail": "凭据请求格式无效，请检查字段类型"})
        return RedactingJSONResponse(
            status_code=422,
            content={"detail": jsonable_encoder(exc.errors())},
        )
    runtime = RuntimeManager(RuntimeSnapshot(manager.active_revision, settings_, container, agent))
    manager.attach_runtime(runtime)
    app.state.runtime = runtime
    app.state.container = RuntimeProxy(runtime, "container")
    app.state.settings = RuntimeProxy(runtime, "settings")
    app.state.agent = RuntimeProxy(runtime, "agent")
    app.state.admin_config = manager

    @app.middleware("http")
    async def _runtime_request(request: Request, call_next):
        # 非管理员的写请求计入 active_operations；若仍有请求执行，配置应用返回 409。
        # 管理接口由 AdminConfigManager 自己持有写锁并调用 begin_apply。
        # 读请求只绑定版本、不阻止切换，因此一轮读响应可在旧快照上完成。
        operation = (
            request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and not request.url.path.startswith("/admin/")
        )
        token = runtime.bind_request(operation=operation)
        if token is None:
            return RedactingJSONResponse(
                status_code=409,
                content={"detail": "配置正在应用，请稍后重试"},
            )
        try:
            return await call_next(request)
        finally:
            runtime.reset_request(token)
            if operation:
                runtime.end_operation()

    # 路由在此集中挂载，便于核对完整鉴权面；新增业务路由必须显式挂 require_auth。
    # 管理员端点各自使用 require_admin；集中挂载本身不会自动保护遗漏依赖的路由。
    # 探针、控制台空壳和 app 级 /docs、/redoc、/openapi.json 保持公开。
    app.include_router(health_api.router)  # 探针豁免：健康检查不该要密钥
    app.include_router(status_api.router)  # 控制台空壳；真实数据 API 仍要求 Bearer
    app.include_router(admin_api.router)  # 端点内部独立校验 require_admin
    app.include_router(chat_api.router, dependencies=[Depends(require_auth)])
    app.include_router(alert_api.router, dependencies=[Depends(require_auth)])
    app.include_router(approvals_api.router, dependencies=[Depends(require_auth)])
    app.include_router(sessions_api.router, dependencies=[Depends(require_auth)])
    app.include_router(inspections_api.router, dependencies=[Depends(require_auth)])
    app.include_router(hosts_api.router, dependencies=[Depends(require_auth)])
    app.include_router(identity_api.router, dependencies=[Depends(require_auth)])
    return app


def _check_auth(settings: Settings, *, require_tokens: bool = True) -> None:
    """校验身份映射与角色；业务 HTTP 始终强制鉴权。
    内部直接构造的空 Token 配置允许装配，但所有业务接口返回 503。
    """
    try:
        # 内部健康测试可显式注入空凭据 Settings，业务端点仍拒绝访问。
        # 文件启动及注入部署管理器时必须有可用身份。
        if require_tokens or (settings.auth.tokens or "").strip():
            ensure_usable(
                settings.auth.tokens,
                approval_mode=settings.auth.approval_mode,
                approvers_raw=settings.auth.approver_identities,
            )
    except AuthConfigError as exc:
        raise RuntimeError(f"鉴权配置有误，拒绝启动：{exc}") from exc

    try:
        admins = set(parse_admins(settings.auth.admin_identities))
        identities = {identity for _, identity in parse_tokens(settings.auth.tokens)}
    except AuthConfigError as exc:
        raise RuntimeError(f"管理员鉴权配置有误，拒绝启动：{exc}") from exc
    unknown_admins = admins - identities
    if unknown_admins:
        raise RuntimeError(
            "管理员身份没有对应 Token，拒绝启动：" + ", ".join(sorted(unknown_admins))
        )

    if identities and not (settings.auth.alert_identity or "").strip():
        logger.warning(
            "auth.alert_identity 为空 —— POST /alert 对**任何**有效密钥开放。"
            "而它会喂给规则引擎（auto_execute: true 的规则直接执行写命令、不经审批），"
            "等于每个聊天用户都能触发写入。建议在 settings.yaml 里设成 webhook 专用身份。"
        )


# 模块级实例：uvicorn src.main:app 直接引用的对象
app = create_app()
