"""规则引擎：加载 config/rules.yaml + 匹配决策 + 经注入执行器执行。

承接原 ops_bot/rules.py（加载/校验）与 remediation.py（匹配/决策/记录）：
- 加载：读 config/rules.yaml，先做 ${ENV} 插值（密码等占位在加载期解析为环境变量值），
  再解析为 ActionRule；动作名不在注入执行器集合中的规则被过滤并告警；
- 决策：按 alertname 匹配（同 alertname 多条规则按文件顺序执行）；resolved 跳过；
  auto_execute=false → 仅命中不做动作（SKIP_MARK 状态）；
- 执行：只依赖 ActionExecutor 接口，execute 结果记录进处置记录；
  审计（真实 SSH 尝试前后）由执行器内部完成（见 plugins/actions/executor.py 与 common/audit.py）。

本模块不感知具体动作实现；executors 由 container 注入（可注入假执行器做单测）。
**本模块不做任何输出**：处置结果的终端打印在 OpsAgent（见 core/agent.py），
规则引擎只负责「匹配 + 决策 + 执行 + 产出记录」，保持纯净可测。
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.interfaces.action import ActionError, ActionExecutor, RuleExecutionGuard
from src.storage.models import ActionRule, Alert, AlertmanagerPayload, RulesFile
from src.utils.config_loader import ROOT_DIR, interpolate
from src.utils.security import redact_sensitive

logger = logging.getLogger("ops_agent.rule_engine")

# 命中规则但 auto_execute=false 的状态标记（与"无规则/执行失败"区分）
SKIP_MARK = "matched_but_auto_execute_disabled"
DUPLICATE_MARK = "duplicate_ignored"
INSTANCE_MISMATCH_MARK = "instance_mismatch"

# 默认规则文件：<仓库根>/config/rules.yaml
DEFAULT_RULES_FILE: Path = ROOT_DIR / "config" / "rules.yaml"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _ProcessExecutionGuard:
    """RuleEngine 默认的单进程幂等守卫。容器启用持久化时会替换成 SQLite 实现。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed: set[tuple[str, str, str, str]] = set()

    def claim(
        self,
        *,
        alert_key: str,
        action: str,
        alert_status: str,
        starts_at: str,
    ) -> bool:
        key = (alert_key, action, alert_status, starts_at)
        with self._lock:
            if key in self._claimed:
                return False
            self._claimed.add(key)
            return True


def _resolve_rules_path(path: str | Path | None) -> Path:
    """规则文件路径：显式入参 > OPS_RULES_FILE 环境变量 > 默认 config/rules.yaml。"""
    if path:
        return Path(path)
    env = os.getenv("OPS_RULES_FILE")
    return Path(env) if env else DEFAULT_RULES_FILE


class RuleEngine:
    """告警 → 规则 → 动作 的匹配与执行。"""

    def __init__(
        self,
        executors: Mapping[str, ActionExecutor] | None = None,
        rules_file: str | Path | None = None,
        execution_guard: RuleExecutionGuard | None = None,
    ) -> None:
        self.executors: dict[str, ActionExecutor] = dict(executors or {})
        self.rules_file: Path = _resolve_rules_path(rules_file)
        self.execution_guard: RuleExecutionGuard = execution_guard or _ProcessExecutionGuard()
        self._rules: list[ActionRule] = []
        self.reload_rules()

    # -- 加载 -------------------------------------------------------------
    @property
    def rules(self) -> list[ActionRule]:
        """当前生效的规则列表（按文件顺序）。"""
        return list(self._rules)

    def reload_rules(self) -> list[ActionRule]:
        """重新加载规则（改完 rules.yaml 后调用，无需重启服务）。"""
        self._rules = self._load(self.rules_file)
        return list(self._rules)

    def _load(self, path: Path) -> list[ActionRule]:
        """读取并校验规则文件，返回可用规则；失败返回空表不抛错。"""
        if not path.is_file():
            logger.warning("规则文件不存在，自动处置不启用: %s", path)
            return []
        try:
            import yaml
        except ImportError:
            logger.error("缺少可选依赖 pyyaml，自动处置不启用：pip install pyyaml")
            return []
        try:
            with open(path, encoding="utf-8") as fh:
                # 先做 ${ENV} 插值（rules.yaml 里密码等以 ${VAR} 占位），再解析
                data = yaml.safe_load(interpolate(fh.read())) or {}
            parsed = RulesFile(**data)
        except Exception as exc:  # yaml 语法错 / pydantic 校验错统一收敛
            logger.error("规则文件解析失败: %s（%s）", path, exc)
            return []

        # 过滤未注入执行器的动作，避免运行时才报错
        unknown = {r.action for r in parsed.rules} - set(self.executors)
        if unknown:
            logger.warning("以下动作未注入执行器，对应规则将被忽略: %s", sorted(unknown))
        rules = [r for r in parsed.rules if r.action in self.executors]

        # auto_execute 是高危路径：没有明确目标 instance 的规则一律拒绝启用。
        # 否则 Prometheus 里来自任意主机的同名告警都可能触发固定目标上的写命令。
        missing_target = [r for r in rules if r.auto_execute and not r.target_instance]
        if missing_target:
            logger.error(
                "以下 auto_execute 规则缺少 target_instance，已禁用: %s",
                [f"{r.alertname}->{r.action}" for r in missing_target],
            )
            rules = [r for r in rules if not (r.auto_execute and not r.target_instance)]

        logger.info(
            "已加载自动处置规则 %d 条: %s",
            len(rules),
            [f"{r.alertname}->{r.action}(auto={r.auto_execute})" for r in rules],
        )
        return rules

    # -- 匹配与执行 -------------------------------------------------------
    def execute_for_payload(self, payload: AlertmanagerPayload) -> list[dict[str, Any]]:
        """对整组 webhook 的每条告警匹配处置。

        - resolved（已解决）告警不参与处置：避免对已恢复的问题重复执行修复；
        - 返回所有处置记录（含"命中但未自动执行"），无规则命中的不包含；
        - 同一条告警命中多条规则时返回多条记录（按文件顺序）。
        """
        records: list[dict[str, Any]] = []
        for alert in payload.alerts:
            if alert.is_resolved:
                logger.debug("告警已解决，跳过自动处置 alertname=%s", alert.alertname)
                continue
            records.extend(self._execute_alert(alert))
        return records

    def _execute_alert(self, alert: Alert) -> list[dict[str, Any]]:
        """对单条告警按 alertname 匹配，逐条执行所有命中规则。"""
        matched = [r for r in self._rules if r.alertname == alert.alertname]
        if not matched:
            logger.debug("告警无匹配规则，跳过自动处置 alertname=%s", alert.alertname)
            return []
        return [self._execute_rule(alert, rule) for rule in matched]

    def _execute_rule(self, alert: Alert, rule: ActionRule) -> dict[str, Any]:
        """对单条「已匹配规则」执行（auto_execute 决策 + 调用执行器），返回处置记录。"""
        record: dict[str, Any] = {
            "alert_key": alert.alert_key,  # 与归档表的关联键（见 storage/models.py）
            "alertname": alert.alertname,
            "severity": alert.severity,
            "instance": alert.instance,
            "matched_rule": rule.action,
            "action": None,
            "params": None,
            "status": None,
            "detail": "",
            "executed_at": _now(),
        }

        if not rule.auto_execute:
            record["status"] = SKIP_MARK
            record["detail"] = f"命中规则(动作={rule.action})，但 auto_execute=false，跳过自动执行"
            logger.warning("告警命中规则但未自动执行: %s %s", alert.alertname, record["detail"])
            return record

        params = dict(rule.params)
        record["action"] = rule.action
        record["params"] = redact_sensitive(params)  # 记录打码副本，日志不含敏感值

        if alert.instance != rule.target_instance:
            record["status"] = INSTANCE_MISMATCH_MARK
            record["detail"] = (
                f"告警实例 {alert.instance!r} 与规则目标 {rule.target_instance!r} 不一致，"
                "拒绝自动执行"
            )
            logger.error("自动处置实例不匹配 %s -> %s: %s", alert.alertname, rule.action, record["detail"])
            return record

        if not self.execution_guard.claim(
            alert_key=alert.alert_key,
            action=rule.action,
            alert_status=alert.status,
            starts_at=alert.startsAt,
        ):
            record["status"] = DUPLICATE_MARK
            record["detail"] = "同一告警事件与动作已执行或已被抢占，跳过重复自动处置"
            logger.warning("自动处置重复事件已跳过 %s -> %s", alert.alertname, rule.action)
            return record

        try:
            result = self.executors[rule.action].execute(params)
            result_text = str(result)
            if len(result_text) > 1000:
                result_text = result_text[:1000] + "…（结果过长已截断）"
            record["status"] = "ok"
            record["detail"] = f"动作执行成功: {result_text}"
            logger.info("自动处置成功 %s -> %s: %s", alert.alertname, rule.action, result_text)
        except ActionError as exc:
            record["status"] = "failed"
            record["detail"] = f"动作执行失败: {exc}"
            logger.error("自动处置失败 %s -> %s: %s", alert.alertname, rule.action, exc)
        except Exception as exc:  # noqa: BLE001 - 单条规则异常不能拖垮整批告警
            record["status"] = "failed"
            record["detail"] = f"动作执行异常: {exc}"
            logger.error("自动处置异常 %s -> %s: %s", alert.alertname, rule.action, exc, exc_info=True)
        return record

    # -- 无规则告警（LLM 分析对象） ----------------------------------------
    def unmatched_alerts(self, payload: AlertmanagerPayload) -> list[Alert]:
        """返回整组通知里「没有自动处理规则」且未解决的告警。

        与 execute_for_payload 互补：这里选出规则表中不存在对应 alertname 的告警，
        供上层把它们交给 LLM 生成根因假设与处理建议。
        """
        known = {r.alertname for r in self._rules}
        return [
            alert
            for alert in payload.alerts
            if not alert.is_resolved and alert.alertname not in known
        ]

