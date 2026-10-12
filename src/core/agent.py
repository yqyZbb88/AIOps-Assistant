"""运维主控制器：编排聊天、告警规则、AI 分析与历史查询。

依赖注入接口，不在此构建模型。webhook 先处置和分析，再批量归档；归档失败
上抛供发送方重试，已发生的远端动作不会撤销。
"""
from __future__ import annotations

import logging
from typing import Any

from src.core.analysis import AlertAnalyzer
from src.core.chat import ChatService
from src.core.rule_engine import RuleEngine
from src.interfaces.approval import ApprovalOutcome, PendingApproval
from src.interfaces.history import (
    AdviceRecord,
    AlertRecord,
    HistoryError,
    HistoryStore,
    RemediationRecord,
)
from src.interfaces.host import HostRegistry
from src.interfaces.llm_client import LLMClient
from src.interfaces.monitor import MonitorSource
from src.interfaces.session import ChatReply
from src.storage.memory import AlertStore
from src.storage.models import AlertAck, AlertmanagerPayload
from src.utils.time import now_iso

logger = logging.getLogger("aiops_assistant.core")

# 最近告警缓冲的默认上限（可经注入 store 覆盖）
DEFAULT_RECENT_LIMIT = 100


class OpsAgent:
    """主控制器：把聊天 / 告警处理 / 查询编排成对 api 层友好的方法。"""

    def __init__(
        self,
        monitor: MonitorSource,
        *,
        store: AlertStore | None = None,
        chat_service: ChatService | None = None,
        rule_engine: RuleEngine | None = None,
        analysis: AlertAnalyzer | None = None,
        llm: LLMClient | None = None,
        history: HistoryStore | None = None,
        hosts: HostRegistry | None = None,
    ) -> None:
        self.monitor = monitor
        self.store = store or AlertStore(maxlen=DEFAULT_RECENT_LIMIT)
        # 状态日志归档（可选）：None = 不落盘，告警接收与查询全部照常
        self.history = history
        self.hosts = hosts
        self.chat_service = chat_service or ChatService(monitor)
        self.rule_engine = rule_engine  # 通常由 container 注入（含执行器）
        self.llm = llm
        # 无规则告警的 AI 分析器；未注入时按注入的 llm 兜底构造
        self.analysis = analysis or AlertAnalyzer(monitor, llm=llm, hosts=hosts)

    # -- 聊天 -----------------------------------------------------------
    def chat(self, message: str) -> str:
        """聊天入口（无会话）：Agent-first + 关键词兜底（详见 ChatService）。"""
        return self.chat_service.reply(message)

    def chat_turn(
        self,
        message: str,
        *,
        session_id: str | None = None,
        user: str | None = None,
    ) -> ChatReply:
        """带会话的一轮：返回回答文本 + 会话 id / 会话名（`/chat` 走这里）。

        `user` 是调用者身份，由 API 层从有效密钥推出（None 仅供可信内部旧入口）。
        `session_id` 未知**或不属于 `user`** 一律抛 SessionNotFoundError（api 层转 404，
        不静默重建、也不区分「不存在」与「不是你的」）。
        """
        return self.chat_service.reply_turn(message, session_id=session_id, user=user)

    def cancel_pending(self, approval_id: str) -> bool:
        """作废一条待审批项（会话被删除时一并作废其未决审批）。"""
        return self.chat_service.cancel_pending(approval_id)

    @property
    def storage_healthy(self) -> bool:
        """持久化是否正常（供 /health 暴露降级）。"""
        return self.chat_service.storage_healthy

    # -- 人工审批（Agent 写命令的强制审核环节） -----------------------------
    def pending_approvals(self, *, strict: bool = False) -> list[PendingApproval]:
        """当前待人工决策的写命令调用（新的在前）；未启用写命令时为空列表。"""
        return self.chat_service.pending_approvals(strict=strict)

    def decide(
        self,
        approval_id: str,
        *,
        action: str,
        reason: str = "",
        decided_by: str | None = None,
    ) -> ApprovalOutcome:
        """提交一次审批决策并续跑；未知/过期/已用过 → ApprovalError（api 层转 404）。"""
        return self.chat_service.decide(
            approval_id,
            action=action,
            reason=reason,
            decided_by=decided_by,
        )

    # -- 告警接收（记录 + 规则处置 + AI 分析） -----------------------------
    def process_alert(self, payload: AlertmanagerPayload) -> AlertAck:
        """记录内存告警、执行匹配规则、分析未匹配项，最后归档并返回 AlertAck。

        AI 失败只记录日志；归档失败上抛。调用方重试时不能假定此前没有执行动作。
        """
        summaries = [self.store.record(alert) for alert in payload.alerts]
        if self.hosts is not None:
            for alert, summary in zip(payload.alerts, summaries):
                resolution = self.hosts.resolve_labels(alert.labels)
                summary["host_id"] = resolution.host_id
                summary["host_resolution"] = resolution.status
        ack = AlertAck(received=len(payload.alerts), alerts=summaries)

        # 1) 规则自动处置（resolved 在引擎内跳过；无规则命中的不产生记录）
        if self.rule_engine is not None:
            remediation = self.rule_engine.execute_for_payload(payload)
            if remediation:
                if self.hosts is not None:
                    by_key = {
                        alert.alert_key: self.hosts.resolve_labels(alert.labels)
                        for alert in payload.alerts
                    }
                    for record in remediation:
                        resolution = by_key.get(record.get("alert_key", ""))
                        if resolution is not None:
                            record["host_id"] = resolution.host_id
                            record["host_resolution"] = resolution.status
                ack.remediation = remediation
                self._print_remediation(remediation)

            # 2) 无自动处理规则的告警 → LLM 根因分析（失败仅日志，不阻塞）
            unmatched = self.rule_engine.unmatched_alerts(payload)
            advice = self.analysis.analyze_many(unmatched)
            if advice:
                ack.ai_advice = advice
                self._print_ai_advice(advice)
        else:
            # 规则引擎未注入（配置未就绪）：仍对无规则告警做 AI 分析
            advice = self.analysis.analyze_many(
                [a for a in payload.alerts if not a.is_resolved]
            )
            if advice:
                ack.ai_advice = advice
                self._print_ai_advice(advice)

        self._archive(payload, ack)

        logger.info(
            "告警处理完成，确认 %d 条，自动处置 %d 条，AI 分析 %d 条",
            ack.received, len(ack.remediation), len(ack.ai_advice),
        )
        return ack

    # -- 状态日志归档（可选能力，未注入时整体跳过） -----------------
    def _archive(self, payload: AlertmanagerPayload, ack: AlertAck) -> None:
        """在同一存储事务中归档本批告警、处置与建议，失败上抛。

        归档位于规则执行和分析之后，不与远端动作构成原子事务；无归档 Store 时跳过。
        """
        if self.history is None:
            return
        try:
            stamp = now_iso()
            alerts = [
                AlertRecord(
                    alert_key=alert.alert_key,
                    alertname=alert.alertname,
                    severity=alert.severity,
                    instance=alert.instance,
                    summary=alert.annotations.get("summary", ""),
                    status=alert.status,
                    starts_at=alert.startsAt,
                    ends_at=alert.endsAt,
                    # 复用 store 打的时间戳（ack.alerts 与 payload.alerts 同序同长，
                    # 由上面那句列表推导保证），避免同一件事记出两个时间
                    received_at=(
                        ack.alerts[idx].get("received_at", stamp)
                        if idx < len(ack.alerts) else stamp
                    ),
                    host_id=(
                        self.hosts.resolve_labels(alert.labels).host_id or ""
                        if self.hosts is not None else ""
                    ),
                    host_resolution=(
                        self.hosts.resolve_labels(alert.labels).status
                        if self.hosts is not None else "unknown"
                    ),
                )
                for idx, alert in enumerate(payload.alerts)
            ]
            remediations = [RemediationRecord(**self._remediation_fields(r)) for r in ack.remediation]
            advice = [AdviceRecord(**self._advice_fields(a)) for a in ack.ai_advice]
            self.history.record_batch(
                alerts=alerts, remediations=remediations, advice=advice
            )
        except HistoryError as exc:
            logger.error("Alert archive failed: %s", exc, exc_info=True)
            raise
        except Exception as exc:
            logger.error("Unexpected alert archive failure: %s", exc, exc_info=True)
            raise HistoryError("Alert archive failed") from exc

    @staticmethod
    def _remediation_fields(record: dict[str, Any]) -> dict[str, Any]:
        """从处置记录 dict 里挑出归档字段（多余键不落库，缺键给默认值）。"""
        return {
            "alert_key": record.get("alert_key", ""),
            "alertname": record.get("alertname", ""),
            "severity": record.get("severity", ""),
            "instance": record.get("instance", ""),
            "matched_rule": record.get("matched_rule", ""),
            "action": record.get("action"),
            "status": record.get("status") or "",
            "detail": record.get("detail", ""),
            "executed_at": record.get("executed_at", ""),
            "host_id": record.get("host_id") or "",
            "execution_id": record.get("execution_id", ""),
        }

    @staticmethod
    def _advice_fields(record: dict[str, Any]) -> dict[str, Any]:
        """从 AI 建议 dict 里挑出归档字段。"""
        return {
            "alert_key": record.get("alert_key", ""),
            "alertname": record.get("alertname", ""),
            "severity": record.get("severity", ""),
            "instance": record.get("instance", ""),
            "advice": record.get("advice", ""),
            "analyzed_at": record.get("analyzed_at", ""),
            "host_id": record.get("host_id") or "",
        }

    @staticmethod
    def _print_remediation(records: list[dict[str, Any]]) -> None:
        """集中记录规则引擎返回的处置结果，不改变其执行状态。"""
        for r in records:
            logger.info(
                "自动处置 alertname=%s status=%s action=%s detail=%s",
                r["alertname"], r["status"], r["action"] or r["matched_rule"], r["detail"],
            )

    @staticmethod
    def _print_ai_advice(advice: list[dict[str, Any]]) -> None:
        """通过日志记录 AI 分析结果。"""
        for entry in advice:
            logger.info(
                "AI 分析 alertname=%s severity=%s instance=%s advice=%s",
                entry["alertname"], entry["severity"], entry["instance"], entry["advice"],
            )

    # -- 查询 -----------------------------------------------------------
    def recent(
        self, limit: int = 20, *, host_id: str | None = None
    ) -> list[dict[str, Any]]:
        """最近接收的告警记录（内存缓冲，进程重启清空；新的在前）。"""
        records = self.store.recent(limit if host_id is None else self.store.maxlen)
        if host_id is not None:
            records = [record for record in records if record.get("host_id") == host_id]
        return records[:limit]

    def active_alerts(self, *, host_id: str | None = None) -> list[dict[str, Any]]:
        """实时查询监控源当前 pending/firing 告警；MonitorError 由 api 层转 502。"""
        if self.hosts is None:
            if host_id is not None:
                raise ValueError("未配置主机清单")
            return self.monitor.active_alerts()
        if host_id is not None:
            host = self.hosts.require(host_id)
        else:
            enabled = self.hosts.enabled()
            if len(enabled) != 1:
                from src.interfaces.host import HostError

                raise HostError("多主机模式查询活动告警必须明确提供 host_id")
            host = enabled[0]
        return self.monitor.active_alerts(target=host.monitor)
