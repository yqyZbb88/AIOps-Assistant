"""依赖注入容器：settings → 插件实例 → OpsAgent（组件惰性装配）。

组装链路（core 只认接口，具体实现从 registry 按 settings 取名实例化）：
    settings
      ├─ prometheus.base_url/timeout ──> PrometheusMonitor（monitor）
      ├─ actions.yaml 写命令目录 ──────> {目录 key: CatalogCommandExecutor} 执行器
      ├─ rules.yaml ──────────────────> RuleEngine(executors)
      ├─ llm.enabled/provider ────────> DeepseekLLMClient | None
      └─ ChatService.assistant_factory：首次聊天时经工厂构建 Agent（惰性，见 core/chat）

惰性边界：
- import 本模块不触发任何 langchain / init.py 导入（Agent 在首次聊天才构建）；
- plugins 包 import 即完成注册，但所有重量级依赖（paramiko / langchain）均惰性加载；
- 组件（monitor / llm / executors / agent）实例化后缓存，全容器共享同一份。
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
    ) -> None:
        self.settings: Settings = settings or load_settings()
        self._config_texts = config_texts or {}
        self._aiops_assistant: OpsAgent | None = None
        self._monitor: MonitorSource | None = None
        self._metrics_config: MetricsConfig | None = None
        self._hosts: HostRegistry | None = None
        self._llm: LLMClient | None = None
        self._executors: dict[str, ActionExecutor] | None = None
        self._execution_guard: RuleExecutionGuard | None = None
        self._diagnostics: DiagnosticSource | None = None
        self._agent_actions: AgentActionSource | None = None
        self._agent_actions_config = None
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
        """加载并缓存唯一主机目标来源；无新配置时合成 legacy-default。"""
        if self._hosts is None:
            from src.utils.hosts_config import load_host_registry, parse_hosts

            self._hosts = (
                parse_hosts(self._config_texts["hosts"])
                if "hosts" in self._config_texts
                else load_host_registry(
                    self.settings.hosts.file or None,
                    legacy_instance=self.metrics_config().instance,
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
            self._llm = cls()
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
        """自动处置幂等守卫；SQLite 可用时跨重启，否则退回进程内。

        存储打开失败属于常见降级路径，此时仍必须做进程内幂等，不能因为 DB 故障
        把重复 webhook 变成重复写命令。

        内存版的保留期**与 SQLite 版同源**：SQLite 版的 remediation_claims 由状态日志
        清理按 history_ttl_hours 过期，故这里把同一个值显式传下去——否则降级时
        `history_ttl_hours` 静默失效，而那是个安全相关的窗口（同一条告警的同一个动作
        还要不要拦）。
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

    def diagnostics(self) -> DiagnosticSource | None:
        """旧单主机兼容诊断源；多主机会话改由 `_host_assistant_factory` 绑定目标。

        目录缺失/关闭/未装 paramiko → None。兼容路径的连接参数留在服务端配置，
        多主机路径则使用 hosts.yaml 的已登记 SSH 目标；两者都不向模型传连接凭据。
        """
        if self._diagnostics is not None:
            return self._diagnostics
        from src.plugins.diagnostics.commands import load_diagnostics_config, parse_diagnostics

        config = (
            parse_diagnostics(self._config_texts["diagnostics"])
            if "diagnostics" in self._config_texts
            else load_diagnostics_config()
        )
        if config is not None and not config.enabled:
            config = None
        if config is None:
            return None
        # paramiko 未装 → 能力降级（不注册诊断工具，Agent 看不到），而非启动失败
        try:
            import paramiko  # noqa: F401
        except ImportError:
            logger.warning("缺少可选依赖 paramiko，只读诊断能力关闭（pip install paramiko）")
            return None
        provider = config.provider or "ssh"
        try:
            cls = registry.diagnostic_class(provider)
        except KeyError:
            available = ", ".join(registry.diagnostic_names()) or "(空)"
            logger.warning(
                "未注册的诊断源 provider=%r（可用: %s），只读诊断关闭",
                provider, available,
            )
            return None
        self._diagnostics = cls(config)
        return self._diagnostics

    def agent_actions(self) -> AgentActionSource | None:
        """旧单主机兼容写命令源；多主机会话由 `_host_assistant_factory` 单独绑定。

        未启用 / 校验失败 / 缺 paramiko / 工具名冲突 → None。兼容路径的连接参数只在
        服务端，模型上下文与工具参数里永无 host/密码/私钥。

        **工具名冲突检测**：写命令 key 会变成 Agent 工具名，而 create_agent 对重名工具
        不报错（静默遮蔽），所以必须在这里拦住。冲突时只关闭本能力——聊天与只读诊断不受影响。
        """
        if self._agent_actions is not None:
            return self._agent_actions
        from src.plugins.agent_actions.catalog import (
            RESERVED_AGENT_TOOL_NAMES,
            colliding_keys,
            load_agent_actions_config,
            parse_agent_actions,
        )

        config = (
            parse_agent_actions(self._config_texts["agent_actions"])
            if "agent_actions" in self._config_texts
            else load_agent_actions_config()
        )
        if config is not None and not config.enabled:
            config = None
        if config is None:
            return None
        # paramiko 未装 → 能力降级（不注册写工具，Agent 看不到），而非启动失败
        try:
            import paramiko  # noqa: F401
        except ImportError:
            logger.warning("缺少可选依赖 paramiko，Agent 写命令能力关闭（pip install paramiko）")
            return None

        occupied = set(RESERVED_AGENT_TOOL_NAMES)
        diag = self.diagnostics()
        if diag is not None:
            occupied |= {spec.key for spec in diag.commands()}
        clashes = colliding_keys((c.key for c in config.commands), occupied)
        if clashes:
            logger.error(
                "Agent 写命令与已占用的工具名冲突，本能力关闭（重名工具会被静默遮蔽）: %s",
                ", ".join(sorted(clashes)),
            )
            return None

        from src.plugins.agent_actions.source import SshAgentActionSource

        self._agent_actions_config = config
        self._agent_actions = SshAgentActionSource(config)
        return self._agent_actions

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
        """会话存储；配置关闭或 SQLite 打不开 → None（持久化能力关闭）。

        三级降级，任一级都不影响服务启动：
        1. 配置关闭            → None（不落盘，行为与改造前一致）；
        2. SQLite 打不开        → **内存实现**（多轮会话仍可用，只是重启即丢）；
        3. 正常                → SqliteSessionStore。
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
        """开库时收敛残留的「待审批」轮，并**分开**报告两类结局。

        **新进程里任何 pending 行必然是孤儿**：待审批项存在 `ApprovalStore`（进程内存），
        checkpoint 线程存在 `InMemorySaver`（同样是进程内存），两者都不可能跨重启存活。
        不处理的话那些行会永远显示「有一条命令等你批」，而 `/approvals` 里根本没有它——
        用户永远批不了。

        两类必须分开说，因为**结局不同、该做的事也不同**：

        - **作废**：决策从没提交过 → 命令一定没执行 → 无需处理；
        - **结果未知**：决策提交过、但进程死在命令跑完之前 → **命令可能已经执行了**，
          这是唯一需要人去翻 `logs/audit.log` 对账的情况，所以用 WARNING 喊出来。
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

    def _assistant_factory(self):
        """聊天 Agent 工厂（惰性）：首次聊天被 ChatService 调用才构建。"""
        from src.agent.assistant import OpsAssistant  # 触发 langchain/init.py 惰性导入

        write_source = self.agent_actions()
        if self.settings.storage.enabled and self.execution_store() is None:
            logger.error("Persistent execution audit unavailable; Agent write tools disabled")
            write_source = None
        approvals = None
        if write_source is not None:
            # 有写命令才建审批存储：容量/时限来自配置（审批本身无开关，恒为强制）
            policy = self._agent_actions_config.approval
            approvals = ApprovalStore(
                maxlen=policy.max_pending, ttl_seconds=policy.timeout_seconds
            )
        return OpsAssistant(
            monitor=self.monitor(),
            metrics_config=self.metrics_config(),
            inspection_store=self.inspection_store(),
            diag=self.diagnostics(),
            agent_actions=write_source,
            approvals=approvals,
            # 审批决策落盘与淘汰后对账都走会话存储（窄接口，见 ApprovalLedger）。
            # 持久化未启用时是 None，那两套机制整体跳过——没有会话表就没有要分辨的东西。
            ledger=self.sessions(),
            debug=self.settings.agent.debug,
            name=self.settings.agent.name,
            recursion_limit=self.settings.agent.recursion_limit,
        )

    def _host_assistant_factory(self, host_id: str):
        """为会话固定主机构造 Agent；所有外部 IO 都从同一 Host 派生。"""
        from src.agent.assistant import OpsAssistant
        from src.plugins.diagnostics.commands import load_diagnostics_config, parse_diagnostics
        from src.plugins.diagnostics.ssh_diagnostics import SshDiagnostics
        from src.plugins.agent_actions.catalog import load_agent_actions_config, parse_agent_actions
        from src.plugins.agent_actions.source import SshAgentActionSource

        host = self.hosts().require(host_id)
        diag = None
        write_source = None
        approvals = None

        if host.ssh is not None:
            diag_config = (
                parse_diagnostics(self._config_texts["diagnostics"])
                if "diagnostics" in self._config_texts
                else load_diagnostics_config()
            )
            if diag_config is not None and not diag_config.enabled:
                diag_config = None
            if diag_config is not None:
                allowed = None if host.legacy else host.capabilities.diagnostics
                if host.legacy or allowed:
                    diag = SshDiagnostics(
                        diag_config,
                        target=host.ssh,
                        allowed_commands=allowed,
                    )

            action_config = (
                parse_agent_actions(self._config_texts["agent_actions"])
                if "agent_actions" in self._config_texts
                else load_agent_actions_config()
            )
            if action_config is not None and not action_config.enabled:
                action_config = None
            if action_config is not None and self.settings.storage.enabled and self.execution_store() is None:
                logger.error("Persistent execution audit unavailable; Agent write tools disabled for host %s", host_id)
                action_config = None
            if action_config is not None:
                allowed_actions = None if host.legacy else host.capabilities.agent_actions
                if host.legacy or allowed_actions:
                    write_source = SshAgentActionSource(
                        action_config,
                        target=host.ssh,
                        allowed_commands=allowed_actions,
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
        return OpsAssistant(
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
            sessions=self.sessions(),  # 未启用/打不开 → None，聊天照常
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
