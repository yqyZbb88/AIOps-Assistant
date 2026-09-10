"""人工审批路由：GET /approvals 待审批列表、POST /approvals/{id} 提交决策。

Agent 的写命令（config/agent_actions.yaml）在执行前会中断，等人工在这里决策。
这是**唯一**能让写命令真正执行的入口。

安全提示（残余风险，部署前须知晓）：
本服务当前没有任何鉴权；/approvals/* 是一个「能执行白名单写命令」的句柄。
缓解措施：审批 id 为随机不可枚举值、server.host 默认仅 127.0.0.1、白名单固定。
**注意：原设计还有一条「出厂 agent_actions.enabled=false」的缓解，但 2026-09-10 起本仓库
已把该能力打开，这条不再成立**——写通道现在是活的。
**若要把服务绑到 0.0.0.0，请先自行加一层鉴权（如共享密钥头），否则任何能访问端口的人
都可能批准写命令。** 详见 README「安全说明」与 PROJECT_HANDOVER 路线图。
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from src.core.agent import OpsAgent
from src.interfaces.approval import ApprovalError, ApprovalUnavailableError

logger = logging.getLogger("ops_agent.api.approvals")

router = APIRouter()


def _agent(request: Request) -> OpsAgent:
    return request.app.state.agent


class ApprovalRequest(BaseModel):
    """/approvals/{id} 请求体。"""

    # 用 Literal 而非 str：edit 之类的值在入参校验阶段就 422，
    # 永远到不了 resume —— 非法负载会**永久毒化** LangGraph 线程（已实测）。
    action: Literal["approve", "reject"] = Field(..., description="审批决策")
    reason: str = Field("", description="审批理由（仅记日志，不影响命令内容）")


class ApprovalResponse(BaseModel):
    """/approvals/{id} 响应体。"""

    approval_id: str
    decision: str
    status: str = Field(..., description="completed=本轮跑完；pending_approval=又触发下一条写命令")
    reply: str
    next_approval_id: str | None = None


@router.get("/approvals", response_model=list[dict[str, Any]])
def list_approvals(request: Request) -> list[dict[str, Any]]:
    """列出当前待人工决策的写命令调用（新的在前）。无写命令能力时返回空列表。"""
    return [p.to_dict() for p in _agent(request).pending_approvals()]


@router.post("/approvals/{approval_id}", response_model=ApprovalResponse)
def decide(approval_id: str, body: ApprovalRequest, request: Request) -> ApprovalResponse:
    """提交一次审批决策并续跑。

    approve → 命令真正执行；reject → 放弃执行（两者都会让 Agent 继续把话说完）。
    审批 id 单次有效：重复提交返回 404。
    """
    logger.info("收到审批决策 id=%s action=%s", approval_id, body.action)
    try:
        outcome = _agent(request).decide(
            approval_id, action=body.action, reason=body.reason
        )
    except ApprovalUnavailableError as exc:
        # 功能没开 ≠ 找错了 id：前者是部署问题，后者是调用问题
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ApprovalResponse(**outcome.to_dict())
