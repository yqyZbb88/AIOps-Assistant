"""OpsAgent：智能运维 Agent 主控制器（依赖注入装配）。

对外能力（原 ops_bot/main.py + bot.py 的编排收敛于此，HTTP 层只做转发）：
- chat(message)：聊天（Agent-first，内部 ChatService 处理回落）；
- process_alert(payload)：Alertmanager 告警接收 → 记录 → 规则处置 → 无规则 AI 分析，
  分析失败 / LLM 不可用仅记日志不阻塞（返回 AlertAck）；
- recent(limit)：最近接收的告警记录（内存缓冲）；
- active_alerts()：实时向监控源查询 pending/firing（MonitorError 上抛，由 api 转 502）。

设计：
- 只依赖注入组件（monitor/store/chat/rule_engine/analysis/llm），不 import 插件实现；
- 自身不建立任何 Agent/LLM（惰性在 ChatService 首次聊天时构建），import 即安全。
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
from src.interfaces.llm_client import LLMClient
from src.interfaces.monitor import MonitorSource
from src.interfaces.session import ChatReply
from src.storage.memory import AlertStore
from src.storage.models import AlertAck, AlertmanagerPayload
from src.utils.time import now_iso

logger = logging.getLogger("ops_agent.core")

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
    ) -> None:
        self.monitor = monitor
        self.store = store or AlertStore(maxlen=DEFAULT_RECENT_LIMIT)
        # 状态日志归档（可选）：None = 不落盘，告警接收与查询全部照常
        self.history = history
        self.chat_service = chat_service or ChatService(monitor)
        self.rule_engine = rule_engine  # 通常由 container 注入（含执行器）
        self.llm = llm
        # 无规则告警的 AI 分析器；未注入时按注入的 llm 兜底构造
        self.analysis = analysis or AlertAnalyzer(monitor, llm=llm)

    # -- 聊天 -----------------------------------------------------------
    def chat(self, message: str) -> str:
        """聊天入口（无会话）：Agent-first + 关键词兜底（详见 ChatService）。"""
        return self.chat_service.reply(message)

    def chat_turn(
        self,
        message: str,
        *,
        session_id: str | None = None,
        session_name: str | None = None,
        user: str = "",
    ) -> ChatReply:
        """带会话的一轮：返回回答文本 + 会话 id / 会话名（`/chat` 走这里）。

        传未知 `session_id` 抛 SessionNotFoundError（api 层转 404，**不静默重建**）；
        传 `session_name` 则是「按名字继续，没有就用这个名字新建」。
        """
        return self.chat_service.reply_turn(
            message, session_id=session_id, session_name=session_name, user=user
        )

    def cancel_pending(self, approval_id: str) -> bool:
        """作废一条待审批项（会话被删除时一并作废其未决审批）。"""
        return self.chat_service.cancel_pending(approval_id)

    @property
    def storage_healthy(self) -> bool:
        """持久化是否正常（供 /health 暴露降级）。"""
        return self.chat_service.storage_healthy

    # -- 人工审批（Agent 写命令的强制审核环节） -----------------------------
    def pending_approvals(self) -> list[PendingApproval]:
        """当前待人工决策的写命令调用（新的在前）；未启用写命令时为空列表。"""
        return self.chat_service.pending_approvals()

    def decide(self, approval_id: str, *, action: str, reason: str = "") -> ApprovalOutcome:
        """提交一次审批决策并续跑；未知/过期/已用过 → ApprovalError（api 层转 404）。"""
        return self.chat_service.decide(approval_id, action=action, reason=reason)

    # -- 告警接收（记录 + 规则处置 + AI 分析） -----------------------------
    def process_alert(self, payload: AlertmanagerPayload) -> AlertAck:
        """处理一条 Alertmanager 通知，返回确认响应 AlertAck。

        流程：逐条记录到 store → 规则引擎处置（auto_execute=true 才动作）→
        无规则告警交给 AI 分析。任何单点失败不影响告警记录与其它告警。
        """
        summaries = [self.store.record(alert) for alert in payload.alerts]
        ack = AlertAck(received=len(payload.alerts), alerts=summaries)

        # 1) 规则自动处置（resolved 在引擎内跳过；无规则命中的不产生记录）
        if self.rule_engine is not None:
            remediation = self.rule_engine.execute_for_payload(payload)
            if remediation:
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

    # -- 状态日志归档（M4 第二批；可选能力，未注入时整体跳过） -----------------
    def _archive(self, payload: AlertmanagerPayload, ack: AlertAck) -> None:
        """把本次处理的产出落进归档表。

        **刻意放在流程最末尾、一次调用、一个事务**，而不是插在三个步骤之间：

        1. 本方法的 docstring 承诺「任何单点失败不影响告警记录与其它告警」。写库若插在
           中间，一次 DB 异常就会中断其后的 `unmatched_alerts` / `analyze_many`——
           等于给告警处理**新增了一个原本不存在的失败模式**。放在末尾，失败面收敛成
           「少一条归档」。
        2. 三张表必须同批一致：一次调用 = 一个 BEGIN IMMEDIATE，不会出现
           「告警写了、处置没写」的半批。
        3. `rule_engine` / `analysis` 是纯决策且被测试锁着「零输出」，在那里插 IO
           会同时破坏可测性与那条护栏。

        失败只记 ERROR：归档不该让告警接收失败。
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
                )
                for idx, alert in enumerate(payload.alerts)
            ]
            remediations = [RemediationRecord(**self._remediation_fields(r)) for r in ack.remediation]
            advice = [AdviceRecord(**self._advice_fields(a)) for a in ack.ai_advice]
            self.history.record_batch(
                alerts=alerts, remediations=remediations, advice=advice
            )
        except HistoryError as exc:
            logger.error("状态日志归档失败（本次告警未归档）: %s", exc)
        except Exception as exc:  # noqa: BLE001 - 归档绝不能影响告警接收
            logger.error("状态日志归档异常（本次告警未归档）: %s", exc, exc_info=True)

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
        }

    @staticmethod
    def _print_remediation(records: list[dict[str, Any]]) -> None:
        """把规则处置结果打印到控制台（阶段先以终端推送；后续可换钉钉/企微）。

        由 RuleEngine 移入：该引擎只有一个调用方（本类），移入后它变回纯决策，
        本模块成为**唯一**的输出口子（与 _print_ai_advice 并列），便于将来整体替换通知渠道。
        """
        for r in records:
            print(
                f"[自动处置] alertname={r['alertname']} status={r['status']} "
                f"action={r['action'] or r['matched_rule']} detail={r['detail']}"
            )

    @staticmethod
    def _print_ai_advice(advice: list[dict[str, Any]]) -> None:
        """把 AI 分析结果打印到控制台（阶段先以终端推送；后续可换钉钉/企微）。"""
        for entry in advice:
            print(
                f"\n[AI 分析] {entry['alertname']} severity={entry['severity']} "
                f"instance={entry['instance']}\n{entry['advice']}\n"
            )

    # -- 查询 -----------------------------------------------------------
    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近接收的告警记录（内存缓冲，进程重启清空；新的在前）。"""
        return self.store.recent(limit)

    def active_alerts(self) -> list[dict[str, Any]]:
        """实时查询监控源当前 pending/firing 告警；MonitorError 由 api 层转 502。"""
        return self.monitor.active_alerts()
