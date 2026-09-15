"""Container 装配链路 + create_app 冒烟（全程离线）。

背景（阶段 8）：container.ops_agent() 与 main.create_app() 此前**从未被任何测试调用**——
整条装配（executors 构建、RuleEngine 装配、provider 解析、各降级分支）没有任何断言。
本文件补上这条链路的护栏：降级写错会静默失效，比崩溃更难发现。

全部用例离线：构造插件不发起网络连接（PrometheusMonitor / DeepseekLLMClient /
SshDiagnostics 都是「构造即就绪、调用才连网」），不 invoke 任何 LLM。
"""
from __future__ import annotations

import logging
import sys

import pytest

from src.container import Container
from src.core.agent import OpsAgent
from src.storage.session import InMemorySessionStore, SqliteSessionStore
from src.utils.config_loader import (
    LLMSettings,
    MonitorSettings,
    PrometheusSettings,
    Settings,
    StorageSettings,
)


# ---------------------------------------------------------------------------
# monitor：未注册 provider → fail-loud
# ---------------------------------------------------------------------------
def test_monitor_unregistered_provider_raises_with_available_list():
    """未注册的 monitor provider 必须 fail-loud，且报错里带可用列表（便于排查拼写）。"""
    c = Container(settings=Settings(monitor=MonitorSettings(provider="nope")))
    with pytest.raises(RuntimeError) as ei:
        c.monitor()
    msg = str(ei.value)
    assert "nope" in msg and "prometheus" in msg  # 报错含可用 provider 列表


def test_monitor_resolved_and_cached():
    """默认 provider=prometheus → 实例化成功；同一容器复用同一实例（缓存）。"""
    c = Container(settings=Settings())
    monitor = c.monitor()
    assert type(monitor).__name__ == "PrometheusMonitor"
    assert c.monitor() is monitor  # 缓存生效，不重复构造


def test_monitor_uses_settings_base_url_and_timeout():
    """prometheus.base_url/timeout 必须真的透传到插件实例（装配参数接错则静默打到错地址）。"""
    settings = Settings(prometheus=PrometheusSettings(base_url="http://p:9090/", timeout=9))
    monitor = Container(settings=settings).monitor()
    assert monitor.base_url == "http://p:9090"  # 尾部斜杠已规整
    assert monitor.timeout == 9


# ---------------------------------------------------------------------------
# llm：两条降级路径都必须「返回 None 而非抛错」
# ---------------------------------------------------------------------------
def test_llm_disabled_returns_none():
    """settings.llm.enabled=false → None（AI 分析关闭，服务照常）。"""
    c = Container(settings=Settings(llm=LLMSettings(enabled=False)))
    assert c.llm() is None


def test_llm_unregistered_provider_returns_none():
    """provider 未注册 → 记警告并返回 None，不抛错（区别于 monitor 的 fail-loud）。"""
    c = Container(settings=Settings(llm=LLMSettings(provider="nope")))
    assert c.llm() is None


def test_llm_enabled_returns_client():
    """默认 deepseek → 客户端实例化成功且被缓存。"""
    c = Container(settings=Settings())
    llm = c.llm()
    assert llm is not None and llm.name == "deepseek"
    assert c.llm() is llm


# ---------------------------------------------------------------------------
# executors：actions.yaml 缺失 → {}（自动处置失效，但不影响服务）
# ---------------------------------------------------------------------------
def test_executors_empty_when_catalog_file_missing(monkeypatch, tmp_path):
    """actions.yaml 缺失 → executors() == {}，不抛错。"""
    monkeypatch.setenv("OPS_ACTIONS_FILE", str(tmp_path / "not_here.yaml"))
    c = Container(settings=Settings())
    assert c.executors() == {}


def test_executors_built_from_real_catalog():
    """默认真实 config/actions.yaml → 构建出非空执行器表，键与目录 key 对应。"""
    executors = Container(settings=Settings()).executors()
    assert executors, "config/actions.yaml 应当至少声明一条写命令"
    for key, ex in executors.items():
        assert ex.name == key  # 键 = 目录 key = rules.yaml 的 action 字段


def test_executors_cached(monkeypatch, tmp_path):
    """executors 结果被缓存（同容器两次调用同一份）。"""
    monkeypatch.setenv("OPS_ACTIONS_FILE", str(tmp_path / "not_here.yaml"))
    c = Container(settings=Settings())
    assert c.executors() is c.executors()


# ---------------------------------------------------------------------------
# diagnostics：paramiko 缺失 → None（能力降级而非启动失败）
# ---------------------------------------------------------------------------
def test_diagnostics_none_when_paramiko_missing(monkeypatch):
    """未装 paramiko → diagnostics() 为 None（Agent 不注册诊断工具），服务照常。

    sys.modules[name]=None 等价「未安装」：任何 import 尝试都抛 ImportError。
    """
    monkeypatch.setitem(sys.modules, "paramiko", None)
    assert Container(settings=Settings()).diagnostics() is None


def test_diagnostics_built_when_config_and_paramiko_present():
    """paramiko 已装 + config/diagnostics.yaml 存在 → 构造诊断源（离线，不连接）。"""
    diag = Container(settings=Settings()).diagnostics()
    assert diag is not None and diag.name == "ssh"
    assert diag.commands(), "diagnostics.yaml 应当至少声明一条只读命令"


# ---------------------------------------------------------------------------
# sessions：配置关闭 → None；SQLite 不可用 → 内存降级（服务照常启动）
# ---------------------------------------------------------------------------
def test_sessions_none_when_disabled():
    """`Settings()` 的 storage.enabled 默认就是 False——测试里直接构造它不会被写脏。"""
    assert Container(settings=Settings()).sessions() is None


def test_sessions_built_when_enabled(tmp_path):
    c = Container(settings=Settings(
        storage=StorageSettings(enabled=True, db_path=str(tmp_path / "ops.db"))
    ))
    assert isinstance(c.sessions(), SqliteSessionStore)


def test_sessions_is_cached(tmp_path):
    """两次调用同一实例：每次新建会让 purge 游标与连接各自为政。"""
    c = Container(settings=Settings(
        storage=StorageSettings(enabled=True, db_path=str(tmp_path / "ops.db"))
    ))
    assert c.sessions() is c.sessions()


def test_sessions_falls_back_to_memory_when_db_unusable(tmp_path):
    """db_path 的父路径被普通文件占住 → 开库失败 → 内存实现（多轮仍可用，只是不持久）。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    c = Container(settings=Settings(
        storage=StorageSettings(enabled=True, db_path=str(blocker / "sub" / "ops.db"))
    ))

    store = c.sessions()

    assert isinstance(store, InMemorySessionStore)
    assert c.sessions() is store, "降级结果也要缓存，否则每个请求重试一次开库并刷 ERROR"


def test_history_none_when_disabled():
    assert Container(settings=Settings()).history() is None


def test_history_built_when_enabled(tmp_path):
    from src.storage.history import SqliteHistoryStore

    c = Container(settings=Settings(
        storage=StorageSettings(enabled=True, db_path=str(tmp_path / "ops.db"))
    ))
    assert isinstance(c.history(), SqliteHistoryStore)


def test_history_has_no_memory_fallback(tmp_path):
    """SQLite 打不开时归档直接关闭，**不做内存降级**。

    归档的价值全在「跨重启可查」；给一个重启即丢的归档表，只会让人误以为记录还在。
    （会话多轮则相反：内存降级仍能提供当次可用的多轮对话，所以那边有降级。）
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    c = Container(settings=Settings(
        storage=StorageSettings(enabled=True, db_path=str(blocker / "sub" / "ops.db"))
    ))

    assert c.history() is None


def test_sessions_and_history_share_one_database(tmp_path):
    """同一个 .db 文件只开一次：两套 store 共用 Database 实例。"""
    c = Container(settings=Settings(
        storage=StorageSettings(enabled=True, db_path=str(tmp_path / "ops.db"))
    ))

    assert c.sessions().db is c.history().db


def test_sessions_reconciles_orphan_pending_on_open(tmp_path):
    """开库时把残留的「待审批」轮标为 abandoned。

    新进程里任何 pending 行**必然是孤儿**：待审批项与 checkpoint 线程都在进程内存里，
    不可能跨重启存活。不处理的话那些行会永远显示「有一条命令等你批」，
    而 /approvals 里根本没有它——用户永远批不了。
    """
    from src.interfaces.session import STATUS_ABANDONED, STATUS_PENDING_APPROVAL
    from src.storage.db import open_database
    from src.storage.session import SqliteSessionStore

    path = tmp_path / "ops.db"
    seed = SqliteSessionStore(open_database(path))
    info = seed.create()
    seed.append_turn(info.session_id, user_content="重启 nginx", assistant_content="⚠️ 待审批",
                     status=STATUS_PENDING_APPROVAL, approval_id="ap1")
    assert seed.messages(info.session_id)[0].status == STATUS_PENDING_APPROVAL

    store = Container(settings=Settings(
        storage=StorageSettings(enabled=True, db_path=str(path))
    )).sessions()

    assert {m.status for m in store.messages(info.session_id)} == {STATUS_ABANDONED}
    assert store.get(info.session_id).pending_approvals == 0


# ---------------------------------------------------------------------------
# ops_agent：完整装配
# ---------------------------------------------------------------------------
def test_ops_agent_assembles_full_chain():
    """完整装配：executors → RuleEngine、ChatService（注入工厂）、AlertAnalyzer。

    这是本文件存在的首要理由——整条装配此前零断言。
    """
    agent = Container(settings=Settings()).ops_agent()
    assert isinstance(agent, OpsAgent)
    assert agent.chat_service is not None
    assert agent.rule_engine is not None
    assert agent.analysis is not None
    # 规则引擎拿到的是真实目录构建的执行器（键集合一致）
    assert set(agent.rule_engine.executors) == set(Container(settings=Settings()).executors())


def test_ops_agent_cached():
    """ops_agent() 缓存：两次调用同一实例（避免重复装配）。"""
    c = Container(settings=Settings())
    assert c.ops_agent() is c.ops_agent()


def test_ops_agent_llm_disabled_still_assembles():
    """llm.enabled=false → 装配照常完成，只是 analysis.llm 为 None（AI 分析关闭）。"""
    agent = Container(settings=Settings(llm=LLMSettings(enabled=False))).ops_agent()
    assert agent.llm is None
    assert agent.analysis.llm is None
    assert agent.chat_service is not None


# ---------------------------------------------------------------------------
# create_app 冒烟（不联网、不 invoke）
# ---------------------------------------------------------------------------
def test_create_app_smoke():
    """main.create_app(settings) 能构建出 app 且 state 就位（路由已挂载）。

    注意用注入的 Settings 而非模块级 app：后者在 import src.main 时就已构建。
    """
    from src.main import create_app

    app = create_app(settings=Settings())
    assert app.state.agent is not None
    assert app.state.container is not None
    assert app.state.settings is not None
    paths = {r.path for r in app.routes}
    # 对外 HTTP 契约：这些路径必须存在（契约本身不允许改动）
    for expected in ("/health", "/chat", "/alert", "/alerts/recent", "/alerts/active"):
        assert expected in paths, f"缺少路由 {expected}"
    # 人工审批（M3 新增，非破坏性扩充）
    for expected in ("/approvals", "/approvals/{approval_id}"):
        assert expected in paths, f"缺少路由 {expected}"
    # 对话历史与状态日志（M4 新增，非破坏性扩充）
    for expected in ("/sessions", "/sessions/{session_id}", "/alerts/history"):
        assert expected in paths, f"缺少路由 {expected}"


def test_create_app_health_endpoint_responds():
    """经 TestClient 真打一次 /health（不触发 Agent/LLM/网络）。"""
    from fastapi.testclient import TestClient

    from src.main import create_app

    with TestClient(create_app(settings=Settings())) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    # storage 是 M4 追加的字段，专为让「持久化降级」可被观测（否则只能翻日志发现）
    assert resp.json() == {"status": "ok", "storage": "ok"}


def test_create_app_health_reflects_history_degradation(tmp_path):
    """归档存储损坏时，/health 必须降级，不能只盯 sessions。"""
    from fastapi.testclient import TestClient

    from src.main import create_app

    settings = Settings(
        storage=StorageSettings(enabled=True, db_path=str(tmp_path / "ops.db"))
    )
    app = create_app(settings=settings)
    app.state.container.history()._healthy = False  # type: ignore[union-attr]
    with TestClient(app) as client:
        resp = client.get("/health")

    assert resp.json() == {"status": "degraded", "storage": "degraded"}


# ---------------------------------------------------------------------------
# agent_actions：Agent 写命令装配（默认关；冲突检测）
# ---------------------------------------------------------------------------
AGENT_ACTIONS_SAMPLE = """
enabled: true
target:
  host: 10.0.0.9
  username: ops
commands:
  {cmd}:
    summary: 测试命令
    template: "true"
    sudo: false
    params: {{}}
"""


def _write_agent_actions(tmp_path, cmd="restart_service"):
    f = tmp_path / "agent_actions.yaml"
    f.write_text(AGENT_ACTIONS_SAMPLE.format(cmd=cmd), encoding="utf-8")
    return f


def test_agent_actions_none_when_disabled(monkeypatch, tmp_path):
    """enabled: false → 能力关闭。

    原用例读仓库文件断言「出厂关」，2026-09-10 仓库改开后不再成立。改为显式喂一份
    关闭的配置，断言的仍是同一个机制（开关语义），且不随仓库当前状态漂移。
    """
    f = tmp_path / "agent_actions_off.yaml"
    f.write_text(
        AGENT_ACTIONS_SAMPLE.format(cmd="restart_service").replace("enabled: true", "enabled: false"),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPS_AGENT_ACTIONS_FILE", str(f))
    assert Container(settings=Settings()).agent_actions() is None


def test_agent_actions_none_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_AGENT_ACTIONS_FILE", str(tmp_path / "not_here.yaml"))
    assert Container(settings=Settings()).agent_actions() is None


def test_agent_actions_built_when_enabled(monkeypatch, tmp_path):
    """启用 + paramiko 在 → 构造写命令源（离线，不连接）。"""
    monkeypatch.setenv("OPS_AGENT_ACTIONS_FILE", str(_write_agent_actions(tmp_path)))
    src = Container(settings=Settings()).agent_actions()
    assert src is not None and [s.key for s in src.commands()] == ["restart_service"]


def test_agent_actions_none_when_paramiko_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_AGENT_ACTIONS_FILE", str(_write_agent_actions(tmp_path)))
    monkeypatch.setitem(sys.modules, "paramiko", None)
    assert Container(settings=Settings()).agent_actions() is None


def test_agent_actions_collision_with_monitor_tool_disables_family(
    monkeypatch, tmp_path, caplog
):
    """与监控工具名冲突 → 本能力关闭（重名工具会被静默遮蔽），但不影响其它能力。"""
    monkeypatch.setenv(
        "OPS_AGENT_ACTIONS_FILE", str(_write_agent_actions(tmp_path, cmd="cpu_usage"))
    )
    c = Container(settings=Settings())
    with caplog.at_level(logging.ERROR):
        assert c.agent_actions() is None
    assert "冲突" in caplog.text
    # 关键：只关这一族，监控与只读诊断照常
    assert c.monitor() is not None
    assert c.diagnostics() is not None


def test_agent_actions_collision_with_diagnostics_key_disables_family(
    monkeypatch, tmp_path
):
    """与只读诊断命令名冲突同样关闭本族（诊断能力本身不受损）。"""
    monkeypatch.setenv(
        "OPS_AGENT_ACTIONS_FILE", str(_write_agent_actions(tmp_path, cmd="service_status"))
    )
    c = Container(settings=Settings())
    assert c.diagnostics() is not None
    assert c.agent_actions() is None


def test_agent_actions_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("OPS_AGENT_ACTIONS_FILE", str(_write_agent_actions(tmp_path)))
    c = Container(settings=Settings())
    assert c.agent_actions() is c.agent_actions()
