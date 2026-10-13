"""告警路由：POST /alert 接收、GET /alerts/recent、GET /alerts/active、GET /alerts/history。

鉴权（config/settings.yaml 的 auth 段）——**本组有个例外，两条规则别混**：

- 整组（含 `POST /alert`）都要求 `Authorization: Bearer <密钥>`；
- **`POST /alert` 还多一层授权**：身份必须等于 `auth.alert_identity`
  （出厂 `config/settings.yaml` 设的是 `alertmanager`），其他有效密钥一律 **403**。
  `/alerts/*` 三个只读端点不受这一层影响。
  注意**代码里的默认值是空串**（= 不限制，fail-open，只记一条 WARNING）——这个字段
  只有 settings.yaml 说了算，别把出厂值当成代码保证（见 `utils/config_loader.py` 的
  `AuthSettings.alert_identity`）。

为什么单单 `/alert` 要设限：这条路径能让 RuleEngine 按 rules.yaml 执行白名单写命令
（`core/agent.py` 的 `process_alert`，`auto_execute: true` 时**不经人工审批**）。
不设限的话，任何持有效密钥的聊天用户都能构造告警去踩那条写路径。

配置 webhook 时必须在 Alertmanager 侧同步设置 Bearer，否则投递会被拒绝：
在 receiver 的 `webhook_configs` 里给它加 `http_config.authorization`
（`type: Bearer` + 密钥，见 `require_alert_identity`）。**两个常见坑**：
① 漏配 → 401，服务侧一条都收不到，只有 Alertmanager 的日志看得到；
② 配了但身份名与 `auth.alert_identity` 对不上 → 403，表现**同样是收不到告警**。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.deps import require_alert_identity
from src.core.agent import OpsAgent
from src.interfaces.history import HistoryError
from src.api.access import require_host, recent_records, visible_host_ids
from src.interfaces.monitor import MonitorError
from src.storage.models import AlertAck, AlertmanagerPayload

logger = logging.getLogger("aiops_assistant.api.alert")

router = APIRouter()


def _agent(request: Request) -> OpsAgent:
    return request.app.state.agent


@router.post("/alert", response_model=AlertAck)
def alert(
    payload: AlertmanagerPayload,
    request: Request,
    identity: str | None = Depends(require_alert_identity),
) -> AlertAck:
    """接收 Alertmanager webhook 推送的告警。

    处理由 OpsAgent 编排：记录 → 规则处置（auto_execute=true 才动作）→
    无规则告警 AI 分析（失败不阻塞）。

    **鉴权：除「密钥有效」外还要求身份是 `auth.alert_identity`（403 否则）。**
    因为这条路径能触发规则引擎执行写命令（`auto_execute: true` 时无需人工审批），
    不能让普通聊天身份构造告警触发写路径。`/alerts/*` 只读查询另按当前主机权限过滤。
    """
    logger.info(
        "收到 Alertmanager 通知 from=%s: %d 条, 整组状态=%s, receiver=%s",
        identity, len(payload.alerts), payload.status, payload.receiver,
    )
    try:
        return _agent(request).process_alert(payload)
    except HistoryError as exc:
        raise HTTPException(status_code=503, detail="Alert processing completed but archive failed; retry delivery") from exc


@router.get("/alerts/recent", response_model=list[dict[str, Any]])
def alerts_recent(
    request: Request,
    limit: int = Query(20, ge=1, le=500),
    host_id: str | None = Query(None, description="按主机 ID 筛选"),
) -> list[dict[str, Any]]:
    """最近接收的告警记录（**内存热缓冲**，进程重启后清空）。

    与 /alerts/history 的区别：这个是「刚发生了什么」的快速视图（有界 deque，
    不落盘、容量小）；history 是落盘归档（可跨重启查、可关联处置与 AI 建议）。
    两者数据源不同，**不是一个东西的两种读法**。
    """
    return recent_records(request, _agent(request).recent, limit=limit, host_id=host_id, timestamp="received_at")


@router.get("/alerts/history", response_model=dict[str, Any])
def alerts_history(
    request: Request,
    limit: int = Query(50, ge=1, le=500, description="每类最多返回条数"),
    host_id: str | None = Query(None, description="按主机 ID 筛选"),
) -> dict[str, Any]:
    """落盘的告警归档：告警 + 处置结果 + AI 建议，各取最近 limit 条。

    持久化未启用时三者均为**空列表**（沿用「能力缺失 = 空而非报错」的约定）；
    存储故障则 503——那是故障，不该伪装成「没有记录」。
    """
    if host_id is not None:
        require_host(request, host_id)
    store = request.app.state.container.history()
    if store is None:
        return {"alerts": [], "remediations": [], "advice": [], "storage": "off"}
    try:
        return {
            "alerts": [r.to_dict() for r in recent_records(request, store.recent_alerts, limit=limit, host_id=host_id, timestamp="received_at")],
            "remediations": [
                r.to_dict() for r in recent_records(request, store.recent_remediations, limit=limit, host_id=host_id, timestamp="executed_at")
            ],
            "advice": [r.to_dict() for r in recent_records(request, store.recent_advice, limit=limit, host_id=host_id, timestamp="analyzed_at")],
            "storage": "ok" if store.healthy else "degraded",
        }
    except HistoryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/alerts/active", response_model=list[dict[str, Any]])
def alerts_active(
    request: Request,
    host_id: str | None = Query(None, description="按主机 ID 查询"),
) -> list[dict[str, Any]]:
    """主动查询监控源当前 pending/firing 告警（实时，非本地缓冲）。

    与 /alerts/recent 的区别：recent 是本服务被动收到的记录；
    active 是实时向监控源 /api/v1/alerts 要当前规则状态。
    """
    try:
        if host_id is not None:
            require_host(request, host_id)
            return _agent(request).active_alerts(host_id=host_id)
        return [alert for current in sorted(visible_host_ids(request, include_disabled=False))
                for alert in _agent(request).active_alerts(host_id=current)]
    except MonitorError as exc:
        logger.warning("查询监控源告警状态失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"监控源查询失败: {exc}") from exc
