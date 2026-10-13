"""依赖装配根：按配置惰性创建插件、Store 和业务组件。

配置切换构造新容器并共享需保留的进程级 Store；重依赖按需加载。数据库失败
缓存为 None，避免每个请求重复开库；各能力按自己的安全边界降级。
"""
from __future__ import annotations

import logging
import hashlib

from src import plugins  # noqa: F401  （导入即把插件注册进 registry）
from src.core.agent import OpsAgent
from src.core.analysis import AlertAnalyzer
from src.core.chat import ChatService
from src.core.rule_engine import RuleEngine
from src.interfaces.action import ActionExecutor, RuleExecutionGuard
from src.interfaces.agent_action import AgentActionSource
from src.interfaces.diagnostic import DiagnosticSource
from src.interfaces.llm_client import LLMClient
from src.interfaces.history import HistoryStore
from src.interfaces.execution import ActionExecutionStore
from src.interfaces.host import HostRegistry
from src.interfaces.inspection import InspectionStore
from src.interfaces.metric import MetricsConfig
from src.interfaces.monitor import MonitorSource
from src.interfaces.session import SessionStore
from src.registry import registry
from src.storage.approval import ApprovalStore
from src.storage.db import Database
from src.storage.memory import AlertStore
from src.utils.config_loader import Settings, load_settings

logger = logging.getLogger("aiops_assistant.container")


class Container:
    """装配容器：按需构造插件 / 组件并缓存（monitor / llm / executors / agent）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        config_texts: dict[str, str] | None = None,
        shared_from: "Container | None" = None,
        credential_specs: dict | None = None,
        model_config: dict | None = None,
    ) -> None:
        self.settings: Settings = settings or load_settings()
        self._config_texts = config_texts or {}
        self._credential_specs = credential_specs or {}
        self._model_config = model_config
        self._aiops_assistant: OpsAgent | None = None
        self._monitor: MonitorSource | None = None
        self._metrics_config: MetricsConfig | None = None
        self._hosts: HostRegistry | None = None
        self._llm: LLMClient | None = None
        self._executors: dict[str, ActionExecutor] | None = None
        self._execution_guard: RuleExecutionGuard | None = None
        self._host_diagnostics: dict[str, DiagnosticSource] = {}
        self._host_actions: dict[str, AgentActionSource] = {}
        self._sessions: SessionStore | None = None
        self._sessions_ready = False
        self._history: HistoryStore | None = None
        self._history_ready = False
        self._inspection_store: InspectionStore | None = None
        self._inspection_store_ready = False
        self._execution_store: ActionExecutionStore | None = None
        self._execution_store_ready = False
        self._inspection_scheduler = None
        self._inspection_checker = None
        self._health_service = None
        self._host_status_service = None
        self._db: Database | None = None
        self._db_ready = False
        self._alert_store: AlertStore | None = None
        if shared_from is not None:
            # 这些对象属于进程生命周期。重建它们会丢掉内存会话/告警/幂等状态，
            # SqliteSessionStore 的构造还会把活跃审批误判成重启后的孤儿。
            self._db = shared_from._database()
            self._db_ready = True
            self._sessions = shared_from.sessions()
            self._sessions_ready = True
            self._history = shared_from.history()
            self._history_ready = True
            self._inspection_store = shared_from.inspection_store()
            self._inspection_store_ready = True
            self._execution_store = shared_from.execution_store()
            self._execution_store_ready = True
            self._execution_guard = shared_from.execution_guard()
            self._inspection_scheduler = shared_from._inspection_scheduler
            self._alert_store = shared_from.aiops_assistant().store

    # -- 组件（惰性 + 缓存） ----------------------------------------------
    def metrics_config(self) -> MetricsConfig:
        """加载统一指标运行配置；file 为空时使用代码目录默认值。"""
        if self._metrics_config is None:
            from src.utils.metrics_config import load_metrics_config, parse_metrics_config

            self._metrics_config = (
                parse_metrics_config(self._config_texts["metrics"])
                if "metrics" in self._config_texts
                else load_metrics_config(self.settings.metrics.file or None)
            )
        return self._metrics_config

    def hosts(self) -> HostRegistry:
        """加载并缓存唯一主机目标来源；所有目标必须显式登记。"""
        if self._hosts is None:
            from src.utils.hosts_config import load_host_registry, parse_hosts

            self._hosts = (
                parse_hosts(self._config_texts["hosts"], credential_overrides=self._credential_specs)
                if "hosts" in self._config_texts
                else load_host_registry(
                    self.settings.hosts.file or None,
                )
            )
        return self._hosts

    def monitor(self) -> MonitorSource:
        """按 settings.monitor.provider 从 registry 取监控源实现并实例化。"""
        if self._monitor is None:
            provider = self.settings.monitor.provider
            try:
                cls = registry.monitor_class(provider)
            except KeyError:
                available = ", ".join(registry.monitor_names()) or "(空)"
                raise RuntimeError(
                    f"未注册的监控源 provider={provider!r}，可用: {available}"
                )
            self._monitor = cls(
                base_url=self.settings.prometheus.base_url,
                timeout=self.settings.prometheus.timeout,
                metrics_config=self.metrics_config(),
            )
        return self._monitor

    def llm(self) -> LLMClient | None:
        """LLM 客户端；settings.llm.enabled=false 或 provider 未注册时返回 None。"""
        if self._llm is None:
            if not self.settings.llm.enabled:
                logger.info("LLM 未启用（settings.llm.enabled=false），跳过 AI 分析")
                return None
            provider = self.settings.llm.provider
            try:
                cls = registry.llm_class(provider)
            except KeyError:
                logger.warning(
                    "未注册的 LLM provider=%r（可用: %s），AI 分析暂不可用",
                    provider, ", ".join(registry.llm_names()) or "(空)",
                )
                return None
            self._llm = cls(model_config=self._model_config) if provider == "openai_compatible" and self._model_config is not None else cls()
        return self._llm

    def executors(self) -> dict[str, ActionExecutor]:
        """把 config/actions.yaml 写命令目录实例化为执行器（键 = 目录 key = rules.action）。

        动作收敛为「写命令目录 + 通用执行器」（见 plugins/actions/catalog、executor），
        不再从 registry 按动作类实例化；目录缺失/关闭/校验失败 → 空（自动处置失效，服务照常）。
        """
        if self._executors is None:
            from src.plugins.actions.catalog import load_actions_catalog, parse_actions
            from src.plugins.actions.executor import build_catalog_executors

            cfg = (
                parse_actions(self._config_texts["actions"])
                if "actions" in self._config_texts
                else load_actions_catalog()
            )
            if cfg is not None and not cfg.enabled:
                cfg = None
            self._executors = build_catalog_executors(cfg) if cfg is not None else {}
            if not self._executors:
                logger.warning("当前没有任何可用动作执行器，规则自动处置将全部失效")
        return self._executors

    def execution_guard(self) -> RuleExecutionGuard:
        """规则幂等守卫：SQLite 可用时持久化，否则使用同保留期的进程内实现。

        幂等记录有 TTL；存储启用但执行 Store 不可用时，规则写路径另行拒绝执行。
        """
        if self._execution_guard is None:
            from src.storage.remediation_guard import (
                InMemoryRuleExecutionGuard,
                SqliteRuleExecutionGuard,
            )

            db = self._database()
            self._execution_guard = (
                SqliteRuleExecutionGuard(db)
                if db is not None
                else InMemoryRuleExecutionGuard(
                    ttl_seconds=self.settings.storage.history_ttl_hours * 3600
                )
            )
        return self._execution_guard

    def diagnostics(self, host_id: str) -> DiagnosticSource | None:
        """显式主机的只读源；没有主机授权不构造连接。"""
        if host_id in self._host_diagnostics:
            return self._host_diagnostics[host_id]
        from src.plugins.diagnostics.commands import load_diagnostics_config, parse_diagnostics
        from src.plugins.diagnostics.ssh_diagnostics import SshDiagnostics
        host = self.hosts().require(host_id)
        config = parse_diagnostics(self._config_texts["diagnostics"]) if "diagnostics" in self._config_texts else load_diagnostics_config()
        if not config or not config.enabled or not host.ssh or not host.capabilities.diagnostics:
            return None
        try:
            import paramiko
        except ImportError:
            return None
        source = SshDiagnostics(config, target=host.ssh, allowed_commands=host.capabilities.diagnostics)
        self._host_diagnostics[host_id] = source
        return source

    def agent_actions(self, host_id: str) -> AgentActionSource | None:
        """显式主机的写命令源；所有命令仍由 Agent 前置审批。"""
        if host_id in self._host_actions:
            return self._host_actions[host_id]
        from src.plugins.agent_actions.catalog import load_agent_actions_config, parse_agent_actions
        from src.plugins.agent_actions.source import SshAgentActionSource
        host = self.hosts().require(host_id)
        config = parse_agent_actions(self._config_texts["agent_actions"]) if "agent_actions" in self._config_texts else load_agent_actions_config()
        if not config or not config.enabled or not host.ssh or not host.capabilities.agent_actions:
            return None
        try:
            import paramiko
        except ImportError:
            return None
        from src.plugins.agent_actions.catalog import colliding_keys, RESERVED_AGENT_TOOL_NAMES
        names = set(RESERVED_AGENT_TOOL_NAMES)
        diag = self.diagnostics(host_id)
        if diag:
            names.update(spec.key for spec in diag.commands())
        if colliding_keys((cmd.key for cmd in config.commands), names):
            logger.error("工具名冲突，关闭此主机的 Agent 写命令源")
            return None
        source = SshAgentActionSource(config, target=host.ssh, allowed_commands=host.capabilities.agent_actions)
        self._host_actions[host_id] = source
        return source

    def _database(self) -> Database | None:
        """打开（并缓存）SQLite 底座；配置关闭或打不开 → None。

        **会话与状态日志共用同一个 Database 实例**（同一个 .db 文件、同一套连接参数）。
        结果含 None 一起缓存：开库失败会打 ERROR 日志，不能每个请求重试一次刷屏。
        """
        if self._db_ready:
            return self._db
        self._db_ready = True

        cfg = self.settings.storage
        if not cfg.enabled:
            logger.info("持久化未启用（storage.enabled=false），会话与告警归档均不落盘")
            return None

        from src.storage.db import open_database

        self._db = open_database(self._resolve_db_path(cfg.db_path))
        return self._db


    def sessions(self) -> SessionStore | None:
        """会话 Store：存储关闭返回 None，开库失败退化到内存，否则使用 SQLite。

        内存会话重启丢失；HTTP 仍需验证身份和会话主机，不能作为匿名入口。
        """
        if self._sessions_ready:
            return self._sessions
        self._sessions_ready = True

        cfg = self.settings.storage
        from src.storage.session import InMemorySessionStore, SqliteSessionStore

        db = self._database()
        if db is None:
            if not cfg.enabled:
                return None
            logger.warning(
                "SQLite 不可用，会话降级为内存实现：多轮对话可用，但重启即丢"
            )
            self._sessions = InMemorySessionStore(
                ttl_seconds=cfg.session_ttl_hours * 3600, max_sessions=cfg.max_sessions
            )
            return self._sessions

        store = SqliteSessionStore(
            db,
            ttl_seconds=cfg.session_ttl_hours * 3600,
            max_sessions=cfg.max_sessions,
            purge_interval_seconds=cfg.purge_interval_seconds,
        )
        self._sessions = store
        self._reconcile_pending(store)
        return store

    def history(self) -> HistoryStore | None:
        """状态日志归档存储；配置关闭或 SQLite 打不开 → None（不落盘）。

        与会话存储不同，这里**没有内存降级实现**：归档的价值全在「跨重启可查」，
        一个重启即丢的归档表只会误导人以为记录还在。故不可用时直接 None。
        """
        if self._history_ready:
            return self._history
        self._history_ready = True

        db = self._database()
        if db is None:
            return None

        from src.storage.history import SqliteHistoryStore

        cfg = self.settings.storage
        self._history = SqliteHistoryStore(
            db,
            ttl_seconds=cfg.history_ttl_hours * 3600,
            max_rows=cfg.max_history_rows,
            purge_interval_seconds=cfg.purge_interval_seconds,
        )
        return self._history

    def inspection_store(self) -> InspectionStore | None:
        """异常巡检归档；存储关闭或 SQLite 不可用时返回 None，巡检日志照常。"""
        if self._inspection_store_ready:
            return self._inspection_store
        self._inspection_store_ready = True
        db = self._database()
        if db is None:
            return None
        from src.storage.inspection import SqliteInspectionStore

        cfg = self.settings.inspection
        self._inspection_store = SqliteInspectionStore(
            db,
            ttl_seconds=cfg.ttl_hours * 3600,
            max_rows=cfg.max_rows,
        )
        return self._inspection_store

    def execution_store(self) -> ActionExecutionStore | None:
        """规则与人工审批写命令共用的执行状态存储。"""
        if self._execution_store_ready:
            return self._execution_store
        self._execution_store_ready = True
        db = self._database()
        if db is None:
            return None
        from src.storage.execution import SqliteActionExecutionStore

        self._execution_store = SqliteActionExecutionStore(db)
        return self._execution_store

    def approval_history(self):
        """审批历史只读取持久化事实；存储关闭/失败时不伪造历史。"""
        from src.storage.approval_history import ApprovalHistory
        db = self._database()
        return ApprovalHistory(db) if db is not None else None

    def inspection_scheduler(self):
        """构造并缓存单进程巡检调度器。"""
        if self._inspection_scheduler is None:
            from src.core.inspection import InspectionScheduler

            self._inspection_scheduler = InspectionScheduler(
                self.inspection_checker(),
                self.settings.inspection,
                store=self.inspection_store(),
            )
        return self._inspection_scheduler

    def inspection_checker(self):
        """每个运行快照独立的巡检器，保留该版本自己的轮询游标。"""
        if self._inspection_checker is None:
            from src.core.inspection import MultiHostInspectionChecker

            self._inspection_checker = MultiHostInspectionChecker(
                self.monitor(), self.hosts(), self.settings.inspection, self.metrics_config()
            )
        return self._inspection_checker

    def health_service(self):
        """构造并缓存非网络健康快照服务；仅 /ready 主动探测 Prometheus。"""
        if self._health_service is None:
            from src.core.health import HealthService

            self._health_service = HealthService(
                self.monitor(),
                storage_enabled=self.settings.storage.enabled,
                stores=lambda: [self.sessions(), self.history(), self.inspection_store()],
                inspection_enabled=self.settings.inspection.enabled,
                scheduler=self.inspection_scheduler,
                version=self.settings.app.version,
            )
        return self._health_service

    def host_status_service(self):
        if self._host_status_service is None:
            from src.core.hosts import HostStatusService

            self._host_status_service = HostStatusService(self.hosts(), self.monitor())
        return self._host_status_service

    @staticmethod
    def _reconcile_pending(store: SessionStore) -> None:
        """重启时收敛内存审批已丢失的 pending 轮次，分别记录 abandoned 与 unknown。

        无决策则当前申请作废；有决策则保守视为结果未知，需核对远端。链式前序动作
        可能已完成，不能由当前申请作废推断整个会话未执行过写操作。
        """
        try:
            abandoned, unknown = store.reconcile_pending(set())
        except Exception as exc:  # noqa: BLE001 - 清理失败不该阻止服务启动
            logger.warning("清理残留待审批会话失败（不影响启动）: %s", exc)
            return
        if abandoned:
            logger.info("已将 %d 条残留的待审批对话标记为作废（审批不跨进程存活）", abandoned)
        if unknown:
            logger.warning(
                "有 %d 条残留的待审批轮**提交过决策但没跑完**：命令可能已经执行过。"
                "它们已被标为「结果未知」而非「未执行」——请对照 logs/audit.log 确认后手工收尾。",
                unknown,
            )

    @staticmethod
    def _resolve_db_path(raw: str):
        """db_path 相对仓库根解析；绝对路径原样使用（便于测试与部署外挂盘）。"""
        from pathlib import Path

        from src.utils.config_loader import ROOT_DIR

        path = Path(raw)
        resolved = path if path.is_absolute() else ROOT_DIR / path
        # 兼容重命名前的默认数据库路径，避免升级后误以为历史数据丢失。
        legacy = ROOT_DIR / "data" / "ops_agent.db"
        if raw == "data/aiops_assistant.db" and not resolved.exists() and legacy.exists():
            logger.warning("检测到旧数据库 %s，继续使用该文件；新默认路径为 %s", legacy, resolved)
            return legacy
        return resolved

    def _model_for_snapshot(self):
        """模型与 Container 绑定同一配置版本，聊天和告警分析共享缓存。"""
        from src.utils.model_loader import get_model
        return get_model(self._model_config)

    def _host_assistant_factory(self, host_id: str):
        """为会话固定主机构造 Agent；所有外部 IO 都从同一 Host 派生。"""
        from src.agent.assistant import OpsAssistant
        from src.plugins.agent_actions.catalog import load_agent_actions_config, parse_agent_actions

        host = self.hosts().require(host_id)
        diag = self.diagnostics(host_id)
        write_source = self.agent_actions(host_id)
        approvals = None
        if write_source is not None and self.settings.storage.enabled and self.execution_store() is None:
            logger.error("持久执行审计不可用，关闭主机 %s 的 Agent 写命令", host_id)
            write_source = None
        if write_source is not None:
            action_config = (
                parse_agent_actions(self._config_texts["agent_actions"])
                if "agent_actions" in self._config_texts
                else load_agent_actions_config()
            )
            approvals = ApprovalStore(
                maxlen=action_config.approval.max_pending,
                ttl_seconds=action_config.approval.timeout_seconds,
            )

        fingerprint_material = "|".join(
            [
                host.host_id,
                host.credential_ref,
                host.monitor.job,
                host.monitor.instance,
                ",".join(sorted(host.capabilities.diagnostics)),
                ",".join(sorted(host.capabilities.agent_actions)),
                ",".join(spec.key for spec in diag.commands()) if diag else "",
                ",".join(spec.key for spec in write_source.commands()) if write_source else "",
            ]
        )
        fingerprint = hashlib.sha256(
            fingerprint_material.encode("utf-8")
        ).hexdigest()
        if write_source is not None:
            write_source.configure_execution(
                self.execution_store(),
                host_id=host.host_id,
                target_summary=f"{host.host_id} ({host.name})",
                config_fingerprint=fingerprint,
            )
        extra = {"model_factory": self._model_for_snapshot} if self._model_config is not None else {}
        return OpsAssistant(
            **extra,
            monitor=self.monitor(),
            metrics_config=self.metrics_config(),
            inspection_store=self.inspection_store(),
            diag=diag,
            agent_actions=write_source,
            approvals=approvals,
            ledger=self.sessions(),
            debug=self.settings.agent.debug,
            name=self.settings.agent.name,
            recursion_limit=self.settings.agent.recursion_limit,
            host_id=host.host_id,
            monitor_target=host.monitor,
            target_summary=f"{host.host_id} ({host.name})",
            config_fingerprint=fingerprint,
        )

    # -- 主控制器 ---------------------------------------------------------
    def aiops_assistant(self) -> OpsAgent:
        """构造（并缓存）主控制器 OpsAgent。"""
        if self._aiops_assistant is not None:
            return self._aiops_assistant

        settings = self.settings
        monitor = self.monitor()
        llm = self.llm()

        rule_engine = RuleEngine(
            executors=self.executors(),
            execution_guard=self.execution_guard(),
            hosts=self.hosts(),
            execution_store=self.execution_store(),
            rules_text=self._config_texts.get("rules"),
            require_execution_store=settings.storage.enabled,
        )
        host_assistant_factory = (
            self._host_assistant_factory if settings.agent.enabled else None
        )
        if host_assistant_factory is None:
            logger.info("聊天 Agent 未启用（settings.agent.enabled=false），仅使用关键词兜底")
        chat_service = ChatService(
            monitor=monitor,
            assistant_factory=None,
            host_assistant_factory=host_assistant_factory,
            hosts=self.hosts(),
            sessions=self.sessions(),  # 关闭时为 None、开库失败时降级内存；HTTP 仍须可验证会话
            message_window=settings.storage.message_window,
            metrics_config=self.metrics_config(),
        )
        analyzer = AlertAnalyzer(
            monitor=monitor,
            llm=llm,
            trend_minutes=settings.alert.trend_minutes,
            metrics_config=self.metrics_config(),
            hosts=self.hosts(),
        )
        self._aiops_assistant = OpsAgent(
            monitor=monitor,
            store=self._alert_store or AlertStore(maxlen=settings.alert.recent_limit),
            chat_service=chat_service,
            rule_engine=rule_engine,
            analysis=analyzer,
            llm=llm,
            history=self.history(),  # 未启用 → None，告警接收照常
            hosts=self.hosts(),
        )
        logger.info(
            "OpsAgent 装配完成: monitor=%s actions=%d llm=%s",
            settings.monitor.provider,
            len(self.executors()),
            getattr(llm, "name", "none"),
        )
        return self._aiops_assistant
