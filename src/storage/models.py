"""数据模型（跨层复用）。

两类模型原先分居 ops_bot/alerts.py 与 ops_bot/rules.py，重构后统一收敛：
- 告警模型：与 Alertmanager webhook JSON 一一对应（Alert / AlertmanagerPayload /
  AlertAck），API 层请求响应与 core 处理共用；
- 规则模型：config/rules.yaml 的「alertname → 动作」映射（ActionRule / RulesFile）。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 告警数据模型（Alertmanager webhook）
# ---------------------------------------------------------------------------
class Alert(BaseModel):
    """单条告警，字段与 Alertmanager 通知 alerts 数组元素对应。"""

    model_config = ConfigDict(extra="ignore")  # 忽略未知字段，兼容版本差异

    status: str = "firing"  # firing（触发中）| resolved（已解决）
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: str = ""
    endsAt: str = ""
    generatorURL: str = ""
    fingerprint: str = ""

    # --- 便捷属性：常用信息不必每次 labels.get(...) ---
    @property
    def alertname(self) -> str:
        """告警名（Prometheus 规则 alert 字段），如 HighCPUUsage。"""
        return self.labels.get("alertname", "unknown")

    @property
    def severity(self) -> str:
        """严重级别（rule 的 labels.severity），如 critical / warning。"""
        return self.labels.get("severity", "none")

    @property
    def instance(self) -> str:
        """触发告警的实例，如 192.168.1.10:9100。"""
        return self.labels.get("instance", "unknown")

    @property
    def is_resolved(self) -> bool:
        """是否已解决。"""
        return self.status == "resolved"


class AlertmanagerPayload(BaseModel):
    """Alertmanager webhook 通知的顶层结构。"""

    model_config = ConfigDict(extra="ignore")

    status: str = ""  # 整组状态：firing / resolved
    receiver: str = ""
    alerts: list[Alert] = Field(default_factory=list)
    externalURL: str = ""
    version: str = ""
    groupKey: str = ""
    truncatedAlerts: int = 0
    # 以下字段当前阶段不参与处理，保留模型定义供后续阶段直接使用
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)


class AlertAck(BaseModel):
    """/alert 接口的确认响应（回给 Alertmanager / 调用方）。"""

    received: int = Field(..., description="本次接收到的告警条数")
    alerts: list[dict[str, Any]] = Field(
        default_factory=list, description="逐条处理摘要（含关键字段）"
    )
    remediation: list[dict[str, Any]] = Field(
        default_factory=list,
        description="自动处置结果记录（告警→规则→动作）",
    )
    ai_advice: list[dict[str, Any]] = Field(
        default_factory=list,
        description="无自动处理规则的告警，LLM 生成的根因假设与处理建议",
    )


# ---------------------------------------------------------------------------
# 处置规则模型（config/rules.yaml）
# ---------------------------------------------------------------------------
class ActionRule(BaseModel):
    """一条「告警 → 动作」规则。"""

    alertname: str = Field(..., description="告警规则名，需与 Prometheus 的 alert 字段一致")
    action: str = Field(..., description="动作名，必须已注册到 actions 注册表")
    params: dict[str, Any] = Field(default_factory=dict, description="传给动作的参数")
    auto_execute: bool = Field(False, description="true=命中即自动执行；false=仅命中提示")
    description: str = Field("", description="规则用途说明")


class RulesFile(BaseModel):
    """规则文件顶层结构。"""

    rules: list[ActionRule] = Field(default_factory=list)
