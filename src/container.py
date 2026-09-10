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
from typing import Any

from src import plugins  # noqa: F401  （导入即把插件注册进 registry）
from src.core.agent import OpsAgent
from src.core.analysis import AlertAnalyzer
from src.core.chat import ChatService
from src.core.rule_engine import RuleEngine
from src.interfaces.diagnostic import DiagnosticSource
from src.interfaces.llm_client import LLMClient
from src.interfaces.monitor import MonitorSource
from src.registry import registry
from src.storage.memory import AlertStore
from src.utils.config_loader import Settings, load_settings

logger = logging.getLogger("ops_agent.container")


class Container:
    """装配容器：按需构造插件 / 组件并缓存（monitor / llm / executors / agent）。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings: Settings = settings or load_settings()
        self._ops_agent: OpsAgent | None = None
        self._monitor: MonitorSource | None = None
        self._llm: LLMClient | None = None
        self._executors: dict[str, Any] | None = None
        self._diagnostics: DiagnosticSource | None = None

    # -- 组件（惰性 + 缓存） ----------------------------------------------
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

    def executors(self) -> dict[str, Any]:
        """把 config/actions.yaml 写命令目录实例化为执行器（键 = 目录 key = rules.action）。

        M1b：动作收敛为「写命令目录 + 通用执行器」（见 plugins/actions/catalog、executor），
        不再从 registry 按动作类实例化；目录缺失/关闭/校验失败 → 空（自动处置失效，服务照常）。
        """
        if self._executors is None:
            from src.plugins.actions.catalog import load_actions_catalog
            from src.plugins.actions.executor import build_catalog_executors

            cfg = load_actions_catalog()
            self._executors = build_catalog_executors(cfg) if cfg is not None else {}
            if not self._executors:
                logger.warning("当前没有任何可用动作执行器，规则自动处置将全部失效")
        return self._executors

    def diagnostics(self) -> DiagnosticSource | None:
        """只读诊断源；config/diagnostics.yaml 缺失/关闭/未装 paramiko → None（Agent 不加诊断工具）。

        凭据来自 config/diagnostics.yaml 的 target（密码 .env 占位插值），实例化后留在本对象内，
        模型上下文与工具参数里永无 host/密码/私钥。
        """
        if self._diagnostics is not None:
            return self._diagnostics
        from src.plugins.diagnostics.commands import load_diagnostics_config

        config = load_diagnostics_config()
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

    def _assistant_factory(self):
        """聊天 Agent 工厂（惰性）：首次聊天被 ChatService 调用才构建。"""
        from src.agent.assistant import OpsAssistant  # 触发 langchain/init.py 惰性导入

        return OpsAssistant(
            monitor=self.monitor(),
            diag=self.diagnostics(),
            debug=self.settings.agent.debug,
            name=self.settings.agent.name,
        )

    # -- 主控制器 ---------------------------------------------------------
    def ops_agent(self) -> OpsAgent:
        """构造（并缓存）主控制器 OpsAgent。"""
        if self._ops_agent is not None:
            return self._ops_agent

        settings = self.settings
        monitor = self.monitor()
        llm = self.llm()

        rule_engine = RuleEngine(executors=self.executors())
        chat_service = ChatService(
            monitor=monitor,
            assistant_factory=self._assistant_factory,
        )
        analyzer = AlertAnalyzer(
            monitor=monitor,
            llm=llm,
            trend_minutes=settings.alert.trend_minutes,
        )
        self._ops_agent = OpsAgent(
            monitor=monitor,
            store=AlertStore(maxlen=settings.alert.recent_limit),
            chat_service=chat_service,
            rule_engine=rule_engine,
            analysis=analyzer,
            llm=llm,
        )
        logger.info(
            "OpsAgent 装配完成: monitor=%s actions=%d llm=%s",
            settings.monitor.provider,
            len(self.executors()),
            getattr(llm, "name", "none"),
        )
        return self._ops_agent
