"""FastAPI 应用入口：装配 container → include api routers。

启动方式（仓库根执行）：
    uvicorn src.main:app --reload --host 127.0.0.1 --port 8000     （推荐，热重载）
    python src/main.py                                              （直跑分支）

对外路由（与旧 ops_bot 一致）：
    GET  /health            健康检查
    POST /chat              聊天 {"user","message"} → {"reply"}
    POST /alert             Alertmanager webhook
    GET  /alerts/recent     最近接收的告警
    GET  /alerts/active     实时活动告警（监控源查询失败转 502）

人工审批（Agent 写命令，config/agent_actions.yaml 启用后才有内容）：
    GET  /approvals             待审批的写命令调用
    POST /approvals/{id}        提交 approve/reject 并续跑
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI

# 兼容直接运行（python src/main.py）：__package__ 为空时把仓库根加入 sys.path
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import alert as alert_api
from src.api import approvals as approvals_api
from src.api import chat as chat_api
from src.api import health as health_api
from src.container import Container
from src.utils.config_loader import Settings
from src.utils.logger import configure_logging

_DESCRIPTION = (
    "智能运维 Agent：关键词/AI 聊天 + Prometheus 指标查询 + 告警状态主动查询"
    " + Alertmanager 告警接收 + 规则自动处置"
)


def create_app(settings: Settings | None = None) -> FastAPI:
    """装配依赖容器并构建 FastAPI 应用（app.state.agent 供路由依赖注入取用）。"""
    container = Container(settings=settings)
    settings_ = container.settings
    configure_logging(
        level=settings_.logging.level,
        audit=settings_.logging.audit,
    )
    agent = container.ops_agent()

    app = FastAPI(
        title=settings_.app.name,
        description=_DESCRIPTION,
        version=settings_.app.version,
    )
    app.state.container = container
    app.state.settings = settings_
    app.state.agent = agent

    app.include_router(health_api.router)
    app.include_router(chat_api.router)
    app.include_router(alert_api.router)
    app.include_router(approvals_api.router)
    return app


# 模块级实例：uvicorn src.main:app 直接引用的对象
app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = app.state.settings
    uvicorn.run(app, host=settings.server.host, port=settings.server.port)
