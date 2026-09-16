"""跨层复用的数据模型。

- 告警模型：对应 Alertmanager webhook JSON 和内部处理结果；
- 规则模型：对应 config/rules.yaml 的 alertname 到动作映射。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# 告警数据模型（Alertmanager webhook）
# ---------------------------------------------------------------------------
class Alert(BaseModel):
    """单条告警，字段与 Alertmanager 通知 alerts 数组元素对应。"""

    model_config = ConfigDict(extra="ignore")  # 忽略未知字段，兼容版本差异

    status: Literal["firing", "resolved"] = "firing"
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

    @property
    def alert_key(self) -> str:
        """归档表之间的关联键（告警 / 处置 / AI 建议三表靠它串起来）。

        **不能直接用 fingerprint**：它默认为空串，而 Alertmanager 之外的来源
        （含全部既有测试的构造）根本不设它——空串当 join key 会把不同告警并成一条。
        故回落成「告警名 + 实例 + 开始时间」的复合键。
        """
        return self.fingerprint or f"{self.alertname}|{self.instance}|{self.startsAt}"


class AlertmanagerPayload(BaseModel):
    """Alertmanager webhook 通知的顶层结构。"""

    model_config = ConfigDict(extra="ignore")

    status: str = ""  # 整组状态：firing / resolved
    receiver: str = ""
    alerts: list[Alert] = Field(default_factory=list, max_length=200)
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
    action: str = Field(..., description="config/actions.yaml 写命令目录的 key")
    params: dict[str, Any] = Field(default_factory=dict, description="传给动作的参数")
    target_instance: str = Field(
        "",
        description="该规则允许处置的 Alertmanager instance；auto_execute=true 时必填且必须精确匹配",
    )
    auto_execute: bool = Field(False, description="true=命中即自动执行；false=仅命中提示")
    description: str = Field("", description="规则用途说明")


class RulesFile(BaseModel):
    """规则文件顶层结构。"""

    rules: list[ActionRule] = Field(default_factory=list)
