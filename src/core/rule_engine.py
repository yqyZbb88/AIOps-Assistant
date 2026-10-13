"""规则引擎：加载 config/rules.yaml，按告警匹配并调用注入的执行器。

- 加载：环境变量插值后解析规则，过滤没有对应执行器的动作；
- 匹配：按 alertname 精确匹配，同一告警可命中多条规则；
- 决策：auto_execute=false 时只记录命中，不执行写命令；
- 执行：只依赖 ActionExecutor 接口，具体实现由容器注入。

本模块只负责匹配、决策、执行和产出记录，不负责终端输出。
"""
from __future__ import annotations

import logging
import os
import threading
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.interfaces.action import ActionError, ActionExecutor, ActionOutcomeUnknown, RuleExecutionGuard
from src.interfaces.host import HostRegistry
from src.interfaces.execution import ActionExecutionStore
from src.plugins.common.endpoint import CONNECTION_FIELDS
from src.storage.models import ActionRule, Alert, AlertmanagerPayload, RulesFile
from src.utils.config_loader import ROOT_DIR, parse_yaml
from src.utils.security import redact_sensitive

logger = logging.getLogger("aiops_assistant.rule_engine")

# 命中规则但 auto_execute=false 的状态标记（与"无规则/执行失败"区分）
SKIP_MARK = "matched_but_auto_execute_disabled"
DUPLICATE_MARK = "duplicate_ignored"
INSTANCE_MISMATCH_MARK = "instance_mismatch"
HOST_UNRESOLVED_MARK = "host_unresolved"
HOST_UNAUTHORIZED_MARK = "host_action_unauthorized"

# 默认规则文件：/app/config/rules.yaml
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
        hosts: HostRegistry | None = None,
        execution_store: ActionExecutionStore | None = None,
        rules_text: str | None = None,
        require_execution_store: bool = False,
    ) -> None:
        self.executors: dict[str, ActionExecutor] = dict(executors or {})
        self.rules_file: Path = _resolve_rules_path(rules_file)
        self.execution_guard: RuleExecutionGuard = execution_guard or _ProcessExecutionGuard()
        self.hosts = hosts
        self.execution_store = execution_store
        self.require_execution_store = require_execution_store
        self._rules_text = rules_text
        self._rules: list[ActionRule] = []
        self._auto_execute_enabled = True
        self.reload_rules()

    # -- 加载 -------------------------------------------------------------
    @property
    def rules(self) -> list[ActionRule]:
        """当前生效的规则列表（按文件顺序）。"""
        return list(self._rules)

    def reload_rules(self) -> list[ActionRule]:
        """重新加载本快照的规则；快照使用冻结的 YAML 文本。"""
        self._rules = (
            self._load_text(self._rules_text)
            if self._rules_text is not None
            else self._load(self.rules_file)
        )
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
            return self._load_text(path.read_text(encoding="utf-8"))
        except Exception as exc:  # yaml 语法错 / pydantic 校验错统一收敛
            logger.error("规则文件解析失败: %s（%s）", path, exc)
            return []

    def _load_text(self, text: str) -> list[ActionRule]:
        """从不可变文本构造规则集，避免旧请求在保存后读到新文件。"""
        parsed = RulesFile(**(parse_yaml(text) or {}))

        # 过滤未注入执行器的动作，避免运行时才报错
        self._auto_execute_enabled = parsed.auto_execute_enabled
        active_rules = [rule for rule in parsed.rules if rule.enabled]
        unknown = {r.action for r in active_rules} - set(self.executors)
        if unknown:
            logger.warning("以下动作未注入执行器，对应规则将被忽略: %s", sorted(unknown))
        rules = [r for r in active_rules if r.action in self.executors]

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
            "host_id": None,
            "host_resolution": "legacy",
        }
        resolution = self.hosts.resolve_labels(alert.labels) if self.hosts is not None else None
        if resolution is not None:
            record["host_id"] = resolution.host_id
            record["host_resolution"] = resolution.status

        if not self._auto_execute_enabled or not rule.auto_execute:
            record["status"] = SKIP_MARK
            reason = "自动处置总开关关闭" if not self._auto_execute_enabled else "auto_execute=false"
            record["detail"] = f"命中规则(动作={rule.action})，但{reason}，跳过自动执行"
            logger.warning("告警命中规则但未自动执行: %s %s", alert.alertname, record["detail"])
            return record

        params = dict(rule.params)
        record["action"] = rule.action
        record["params"] = redact_sensitive(params)  # 记录打码副本，日志不含敏感值

        if resolution is not None and resolution.host is None:
            record["status"] = HOST_UNRESOLVED_MARK
            record["detail"] = (
                f"告警无法唯一归属登记主机（{resolution.status}），拒绝自动执行"
            )
            logger.error("自动处置主机解析失败 %s -> %s: %s", alert.alertname, rule.action, record["detail"])
            return record

        host = resolution.host if resolution is not None else None
        if host is not None and rule.action not in host.capabilities.rule_actions:
            record["status"] = HOST_UNAUTHORIZED_MARK
            record["detail"] = f"主机 {host.host_id} 未授权规则动作 {rule.action}，拒绝自动执行"
            logger.error("自动处置未授权 %s -> %s: %s", alert.alertname, rule.action, record["detail"])
            return record

        if host is not None and host.ssh is None:
            record["status"] = HOST_UNAUTHORIZED_MARK
            record["detail"] = f"主机 {host.host_id} 未配置 SSH，拒绝自动执行"
            return record

        expected_instance = host.monitor.instance if host is not None else rule.target_instance
        if alert.instance != expected_instance or (
            rule.target_instance and rule.target_instance != expected_instance
        ):
            record["status"] = INSTANCE_MISMATCH_MARK
            record["detail"] = (
                f"告警实例 {alert.instance!r}、登记目标 {expected_instance!r} "
                f"与规则目标 {rule.target_instance!r} 不一致，"
                "拒绝自动执行"
            )
            logger.error("自动处置实例不匹配 %s -> %s: %s", alert.alertname, rule.action, record["detail"])
            return record

        if self.require_execution_store and self.execution_store is None:
            record["status"] = "failed"
            record["detail"] = "Persistent execution audit is unavailable; action refused"
            logger.error("Rule action refused without persistent execution audit: %s", rule.action)
            return record

        execution = None
        atomic_store = (
            self.execution_store
            if host is not None
            and self.execution_store is not None
            and getattr(self.execution_guard, "db", None) is not None
            and getattr(self.execution_guard, "db", None) is getattr(self.execution_store, "db", None)
            and callable(getattr(self.execution_store, "claim_rule", None))
            else None
        )
        if self.execution_store is not None and host is not None:
            rule_fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "host_id": host.host_id,
                        "action": rule.action,
                        "params": params,
                        "target_instance": rule.target_instance,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            create_args = dict(
                channel="rule",
                idempotency_key=(
                    f"rule:{host.host_id}:{alert.alert_key}:{rule.action}:"
                    f"{alert.status}:{alert.startsAt}"
                ),
                host_id=host.host_id,
                action_name=rule.action,
                params={k: v for k, v in params.items() if k not in CONNECTION_FIELDS},
                target_summary=f"{host.host_id} ({host.name})",
                config_fingerprint=rule_fingerprint,
                alert_key=alert.alert_key,
                status="running",
            )
            try:
                if atomic_store is not None:
                    execution = atomic_store.claim_rule(
                        action=rule.action,
                        alert_status=alert.status,
                        starts_at=alert.startsAt,
                        **create_args,
                    )
                else:
                    if self.execution_guard.claim(
                        alert_key=alert.alert_key,
                        action=rule.action,
                        alert_status=alert.status,
                        starts_at=alert.startsAt,
                    ):
                        execution = self.execution_store.create(**create_args)
            except Exception as exc:
                record["status"] = "failed"
                record["detail"] = f"Execution claim or audit creation failed; action refused: {exc}"
                logger.error("Rule execution claim failed: %s", exc, exc_info=True)
                return record
            if execution is None:
                record["status"] = DUPLICATE_MARK
                record["detail"] = "Alert action already claimed or recorded; duplicate ignored"
                return record
            record["execution_id"] = execution.execution_id
        elif not self.execution_guard.claim(
            alert_key=alert.alert_key,
            action=rule.action,
            alert_status=alert.status,
            starts_at=alert.startsAt,
        ):
            record["status"] = DUPLICATE_MARK
            record["detail"] = "Alert action already claimed; duplicate ignored"
            return record

        try:
            if host is None:
                result = self.executors[rule.action].execute(params)
            else:
                semantic = {k: v for k, v in params.items() if k not in CONNECTION_FIELDS}
                result = self.executors[rule.action].execute_on(host.ssh, semantic)
            result_text = str(result)
            if len(result_text) > 1000:
                result_text = result_text[:1000] + "…（结果过长已截断）"
            record["status"] = "ok"
            record["detail"] = f"动作执行成功: {result_text}"
            if execution is not None:
                self._finish_execution(execution.execution_id, "succeeded", result_text)
            logger.info("自动处置成功 %s -> %s: %s", alert.alertname, rule.action, result_text)
        except ActionOutcomeUnknown as exc:
            record["status"] = "unknown"
            record["detail"] = f"动作执行结果未知：{exc}；请核对目标状态，勿自动重试"
            if execution is not None:
                self._finish_execution(execution.execution_id, "unknown", str(exc))
            logger.error("Rule action outcome unknown %s -> %s: %s", alert.alertname, rule.action, exc)
        except ActionError as exc:
            record["status"] = "failed"
            record["detail"] = f"动作执行失败: {exc}"
            if execution is not None:
                self._finish_execution(execution.execution_id, "failed", str(exc))
            logger.error("自动处置失败 %s -> %s: %s", alert.alertname, rule.action, exc)
        except Exception as exc:  # noqa: BLE001 - 单条规则异常不能拖垮整批告警
            record["status"] = "failed"
            record["detail"] = f"动作执行异常: {exc}"
            if execution is not None:
                self._finish_execution(execution.execution_id, "failed", str(exc))
            logger.error("自动处置异常 %s -> %s: %s", alert.alertname, rule.action, exc, exc_info=True)
        return record

    def _finish_execution(self, execution_id: str, status: str, detail: str) -> None:
        try:
            self.execution_store.finish(execution_id, status=status, detail=detail)
        except Exception as exc:
            # SSH 结果已经产生；审计收尾失败不能触发重试，更不能拖垮整批告警。
            logger.error(
                "执行状态收尾失败 execution_id=%s（保留 running，按结果未知处理）: %s",
                execution_id,
                exc,
            )

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

