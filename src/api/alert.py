"""告警路由：POST /alert 接收、GET /alerts/recent、GET /alerts/active、GET /alerts/history。

鉴权（config/settings.yaml 的 auth 段）——**本组有个例外，两条规则别混**：

- 整组（含 `POST /alert`）都要求 `Authorization: Bearer <密钥>`；
- **`POST /alert` 还多一层授权**：身份必须是 `auth.alert_identity`（默认 `alertmanager`），
  其他有效密钥一律 **403**。`/alerts/*` 三个只读端点不受这一层影响。

为什么单单 `/alert` 要设限：这条路径能让 RuleEngine 按 rules.yaml 执行白名单写命令
（`core/agent.py` 的 `process_alert`，`auto_execute: true` 时**不经人工审批**）。
不设限的话，任何持有效密钥的聊天用户都能构造告警去踩那条写路径。

⚠️ 因此打开 auth 之后，**必须在 Alertmanager 侧同步配置**，否则告警会被静默挡住：
在 receiver 的 `webhook_configs` 里给它加 `http_config.authorization`
（`type: Bearer` + 密钥，见 `require_alert_identity`）。**两个常见坑**：
① 漏配 → 401，服务侧一条都收不到，只有 Alertmanager 的日志看得到；
② 配了但身份名与 `auth.alert_identity` 对不上 → 403，表现**同样是收不到告警**。
完整配方与排查见 docs/auth.md。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.deps import require_alert_identity
from src.core.agent import OpsAgent
from src.interfaces.history import HistoryError
from src.interfaces.monitor import MonitorError
from src.storage.models import AlertAck, AlertmanagerPayload

logger = logging.getLogger("ops_agent.api.alert")

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
    不能让人人都能构造告警打进来。本组其他路由（`/alerts/*`）仍是「任意有效密钥」。
    """
    logger.info(
        "收到 Alertmanager 通知 from=%s: %d 条, 整组状态=%s, receiver=%s",
        identity, len(payload.alerts), payload.status, payload.receiver,
    )
    return _agent(request).process_alert(payload)


@router.get("/alerts/recent", response_model=list[dict[str, Any]])
def alerts_recent(request: Request, limit: int = 20) -> list[dict[str, Any]]:
    """最近接收的告警记录（**内存热缓冲**，进程重启后清空）。

    与 /alerts/history 的区别：这个是「刚发生了什么」的快速视图（有界 deque，
    不落盘、容量小）；history 是落盘归档（可跨重启查、可关联处置与 AI 建议）。
    两者数据源不同，**不是一个东西的两种读法**。
    """
    return _agent(request).recent(limit)


@router.get("/alerts/history", response_model=dict[str, Any])
def alerts_history(
    request: Request,
    limit: int = Query(50, ge=1, le=500, description="每类最多返回条数"),
) -> dict[str, Any]:
    """落盘的告警归档：告警 + 处置结果 + AI 建议，各取最近 limit 条。

    持久化未启用时三者均为**空列表**（沿用「能力缺失 = 空而非报错」的约定）；
    存储故障则 503——那是故障，不该伪装成「没有记录」。
    """
    store = request.app.state.container.history()
    if store is None:
        return {"alerts": [], "remediations": [], "advice": [], "storage": "off"}
    try:
        return {
            "alerts": [r.to_dict() for r in store.recent_alerts(limit=limit)],
            "remediations": [r.to_dict() for r in store.recent_remediations(limit=limit)],
            "advice": [r.to_dict() for r in store.recent_advice(limit=limit)],
            "storage": "ok" if store.healthy else "degraded",
        }
    except HistoryError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/alerts/active", response_model=list[dict[str, Any]])
def alerts_active(request: Request) -> list[dict[str, Any]]:
    """主动查询监控源当前 pending/firing 告警（实时，非本地缓冲）。

    与 /alerts/recent 的区别：recent 是本服务被动收到的记录；
    active 是实时向监控源 /api/v1/alerts 要当前规则状态。
    """
    try:
        return _agent(request).active_alerts()
    except MonitorError as exc:
        logger.warning("查询监控源告警状态失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"监控源查询失败: {exc}") from exc
