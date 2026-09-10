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
from src.interfaces.llm_client import LLMClient
from src.interfaces.monitor import MonitorSource
from src.storage.memory import AlertStore
from src.storage.models import AlertAck, AlertmanagerPayload

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
    ) -> None:
        self.monitor = monitor
        self.store = store or AlertStore(maxlen=DEFAULT_RECENT_LIMIT)
        self.chat_service = chat_service or ChatService(monitor)
        self.rule_engine = rule_engine  # 通常由 container 注入（含执行器）
        self.llm = llm
        # 无规则告警的 AI 分析器；未注入时按注入的 llm 兜底构造
        self.analysis = analysis or AlertAnalyzer(monitor, llm=llm)

    # -- 聊天 -----------------------------------------------------------
    def chat(self, message: str) -> str:
        """聊天入口：Agent-first + 关键词兜底（详见 ChatService）。"""
        return self.chat_service.reply(message)

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

        logger.info(
            "告警处理完成，确认 %d 条，自动处置 %d 条，AI 分析 %d 条",
            ack.received, len(ack.remediation), len(ack.ai_advice),
        )
        return ack

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
