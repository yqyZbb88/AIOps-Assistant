"""ChatService / OpsAgent：Agent-first 回落、关键词兜底、告警编排（离线）。"""
from __future__ import annotations

import threading
import time

from src.core.agent import OpsAgent
from src.core.analysis import AlertAnalyzer
from src.core.chat import ChatService, DEFAULT_REPLY
from src.core.rule_engine import RuleEngine, SKIP_MARK
from src.interfaces.action import ActionExecutor, ActionError
from src.interfaces.metric import MetricRuntimeConfig, MetricSurfaceConfig, MetricsConfig
from src.interfaces.monitor import MonitorError

from tests.conftest import FakeLLM, make_alert, make_payload


def test_chat_fallback_cpu(fakemonitor):
    # 无 Agent 工厂 → 直接走关键词兜底
    svc = ChatService(fakemonitor)
    reply = svc.reply("CPU 高不高？")
    assert "CPU 使用率" in reply and "45.0%" in reply


def test_chat_fallback_no_data(fakemonitor):
    fakemonitor._memory = None
    svc = ChatService(fakemonitor)
    assert "暂无数据" in svc.reply("内存")


def test_chat_fallback_monitor_error(fakemonitor):
    fakemonitor._raise_cpu = True
    svc = ChatService(fakemonitor)
    reply = svc.reply("CPU")
    assert reply.startswith("查询 CPU 使用率失败")


def test_chat_and_tools_follow_metric_surface_config(fakemonitor):
    config = MetricsConfig(
        metrics={
            "cpu.usage": MetricRuntimeConfig(
                metric_id="cpu.usage",
                surfaces=MetricSurfaceConfig(chat=True),
            ),
            "memory.usage": MetricRuntimeConfig(
                metric_id="memory.usage",
                surfaces=MetricSurfaceConfig(chat=False),
            ),
        }
    )

    svc = ChatService(fakemonitor, metrics_config=config)
    assert "CPU 使用率" in svc.reply("CPU")
    assert svc.reply("内存") == DEFAULT_REPLY

    from src.agent.tools import build_tools

    assert [tool.name for tool in build_tools(fakemonitor, config)] == ["cpu_usage"]


def test_chat_fallback_disk_and_alerts_and_status(fakemonitor):
    fakemonitor._disks = [{"mountpoint": "/", "usage": 88.1}]
    fakemonitor._alerts = [
        {"alertname": "HighCPUUsage", "severity": "critical", "instance": "h:9100",
         "state": "firing", "value": 92.0, "summary": "cpu busy"},
    ]
    svc = ChatService(fakemonitor)
    assert "88.1%" in svc.reply("磁盘空间")
    assert "HighCPUUsage" in svc.reply("有告警吗")
    assert "系统运行正常" in svc.reply("系统状态如何")
    assert "可用能力" in svc.reply("帮助")


def test_chat_unmatched_message_returns_default(fakemonitor):
    svc = ChatService(fakemonitor)
    assert svc.reply("讲个笑话") == DEFAULT_REPLY


def test_ensure_assistant_builds_once_under_concurrency(fakemonitor):
    """并发首聊只能构建一次 Agent（ChatService 是单例，路由跑在线程池里）。

    构建两次的后果**不只是**多一个对象：工厂里还会各建一份 ApprovalStore 与
    InMemorySaver，于是待审批项与 checkpoint 线程分散到两个实例——表现为
    「刚批准的命令找不到线程」。修复前本用例会稳定失败。
    """
    built: list[int] = []

    class FakeAssistant:
        def reply(self, message: str) -> str:
            return f"[Agent]{message}"

    def factory():
        built.append(1)
        time.sleep(0.05)  # 放大竞态窗口：没有锁时 8 个线程会全部挤进来
        return FakeAssistant()

    svc = ChatService(fakemonitor, assistant_factory=factory)
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(svc.reply("hi")))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(built) == 1, f"工厂被调用了 {len(built)} 次（应为 1）"
    assert results == ["[Agent]hi"] * 8


def test_chat_agent_first_then_fallback(fakemonitor):
    """Agent 工厂成功 → 用 Agent 回复；失败 → 关键词/默认回落。"""
    class FakeAssistant:
        def reply(self, message: str) -> str:
            return f"[Agent]{message}"

    def good_factory():
        return FakeAssistant()

    def broken_factory():
        raise RuntimeError("langchain 不可用（模拟）")

    good = ChatService(fakemonitor, assistant_factory=good_factory)
    assert good.reply("你好") == "[Agent]你好"

    broken = ChatService(fakemonitor, assistant_factory=broken_factory)
    assert broken.reply("你好") == DEFAULT_REPLY   # Agent 不可用→默认
    assert broken.reply("CPU")                     # 后续关键词仍可用
    assert "CPU 使用率" in broken.reply("CPU")


def test_chat_falls_back_when_agent_reply_raises(fakemonitor):
    """Agent 构建成功但 reply() 抛异常（网络抖动等）→ 本轮回落关键词。

    阶段 8 补：此前只覆盖了「工厂失败」，这条分支无测试。
    与工厂失败的区别：**不**标记 broken，下次仍会重试 Agent（见下一个用例）。
    """
    class BoomAssistant:
        def reply(self, message: str) -> str:
            raise RuntimeError("网络抖动（模拟）")

    svc = ChatService(fakemonitor, assistant_factory=lambda: BoomAssistant())
    # 命中关键词 → 走兜底规则
    assert "CPU 使用率" in svc.reply("CPU 高不高")
    # 未命中关键词 → 默认回复
    assert svc.reply("讲个笑话") == DEFAULT_REPLY


def test_agent_retried_after_reply_exception(fakemonitor):
    """reply() 异常不标记 broken：Agent 恢复后下次调用仍走 Agent。

    这刻意区别于工厂失败（工厂失败=环境不可用，标记 broken 不再重试；
    reply 失败=单次调用失败，如网络抖动，重试才有意义）。
    """
    calls = {"n": 0}

    class FlakyAssistant:
        def reply(self, message: str) -> str:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("网络抖动（模拟）")
            return f"[Agent]{message}"

    svc = ChatService(fakemonitor, assistant_factory=lambda: FlakyAssistant())
    assert svc.reply("你好") == DEFAULT_REPLY   # 第 1 次失败 → 回落
    assert svc.reply("你好") == "[Agent]你好"    # 第 2 次恢复 → 仍走 Agent


def test_factory_failure_marks_broken_and_stops_retrying(fakemonitor):
    """工厂失败 → 标记 broken，后续不再重试工厂（避免每次聊天都重复昂贵构建）。"""
    calls = {"n": 0}

    def broken_factory():
        calls["n"] += 1
        raise RuntimeError("langchain 不可用（模拟）")

    svc = ChatService(fakemonitor, assistant_factory=broken_factory)
    assert svc.reply("CPU")            # 第 1 次：尝试构建 → 失败 → 兜底
    assert svc.reply("CPU")            # 第 2 次：已 broken，直接兜底
    assert calls["n"] == 1             # 工厂只被调用一次


def test_chat_without_factory_never_calls_agent(fakemonitor):
    """未注入工厂 → _agent_broken 初始为 True，直接关键词兜底（无 Agent 的部署形态）。"""
    svc = ChatService(fakemonitor)
    assert svc._agent_broken is True
    assert "CPU 使用率" in svc.reply("CPU")


def test_agent_tools_share_text_with_fallback(fakemonitor):
    """M2 同源回归：Agent 工具与关键词兜底转发同一批 capabilities 纯函数，产出一致。

    兜底（无 Agent 工厂直接走关键词）与 build_tools 里同名工具，在相同监控数据下
    必须返回**完全相同**的文本——证明「单一实现」，措辞只在 src/core/capabilities.py 一处改。
    """
    from src.agent.tools import build_tools

    def fallback_and_tool(text, name):
        fb = ChatService(fakemonitor).reply(text)
        (tool,) = [t for t in build_tools(fakemonitor) if t.name == name]
        return fb, tool.invoke({})

    # 正常有数据：工具在同源文案后追加统一的数据时效说明。
    fb, out = fallback_and_tool("CPU 高不高", "cpu_usage")
    assert fb == "CPU 使用率：45.0%"
    assert out.startswith(fb)
    assert "[数据时效]" in out

    # 无数据：内存工具保留同源前缀并追加时效说明。
    fakemonitor._memory = None
    fb, out = fallback_and_tool("内存占用如何", "memory_usage")
    assert "暂无数据" in fb
    assert out.startswith(fb) and "[数据时效]" in out

    # 监控失败：CPU 工具保留同源失败文案前缀。
    fakemonitor._raise_cpu = True
    fb, out = fallback_and_tool("CPU", "cpu_usage")
    assert fb.startswith("查询 CPU 使用率失败")
    assert out.startswith(fb) and "[数据时效]" in out

    # 磁盘 / 告警：同样同源
    fakemonitor._raise_cpu = False
    fakemonitor._disks = [{"mountpoint": "/", "usage": 88.1}]
    fakemonitor._alerts = [
        {"alertname": "HighCPUUsage", "severity": "critical", "instance": "h:9100",
         "state": "firing", "value": 92.0, "summary": "cpu busy"},
    ]
    fb, out = fallback_and_tool("磁盘", "disk_usage")
    assert out.startswith(fb) and "88.1%" in fb and "[数据时效]" in out
    fb, out = fallback_and_tool("有告警吗", "active_alerts")
    assert out.startswith(fb) and "HighCPUUsage" in fb and "[数据时效]" in out


# ---------------------------------------------------------------------------
# OpsAgent 回归测试
# ---------------------------------------------------------------------------
def test_aiops_assistant_chat_fallback(fakemonitor):
    agent = OpsAgent(monitor=fakemonitor)
    assert "CPU 使用率" in agent.chat("帮我看看CPU忙不忙")
    assert agent.chat("你好") == DEFAULT_REPLY


def test_aiops_assistant_process_alert_record_and_store(fakemonitor, fakellm):
    """记录 → 规则处置(ok/fail) → 无规则 AI 分析 全链路（假组件，离线）。"""
    class OkExecutor(ActionExecutor):
        name = "ok_action"
        description = "fake"
        def execute(self, params: dict) -> dict:
            return {"ok": True, "host": params["host"]}

    rules_yaml = """\
rules:
  - alertname: HighCPUUsage
    action: ok_action
    target_instance: h:9100
    params: {host: h1}
    auto_execute: true
  - alertname: HighCPUUsage
    action: ok_action
    target_instance: h:9100
    params: {host: h1}
    auto_execute: false
"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        rf = Path(td) / "rules.yaml"
        rf.write_text(rules_yaml, encoding="utf-8")
        engine = RuleEngine(executors={"ok_action": OkExecutor()}, rules_file=rf)

        analyzer = AlertAnalyzer(fakemonitor, llm=fakellm)
        agent = OpsAgent(
            monitor=fakemonitor,
            rule_engine=engine,
            analysis=analyzer,
            llm=fakellm,
        )

        # 一条命中规则、一条无规则 → 记录 + 处置 + AI 分析
        payload = make_payload(
            make_alert("HighCPUUsage", instance="h:9100"),
            make_alert("MysteryAlert", instance="h:9100"),
        )
        ack = agent.process_alert(payload)
        assert ack.received == 2
        assert len(ack.alerts) == 2
        # 命中两条规则（true 执行 + false skip）
        assert [r["status"] for r in ack.remediation] == ["ok", SKIP_MARK]
        # 无规则告警进入 AI 分析
        assert len(ack.ai_advice) == 1
        assert ack.ai_advice[0]["alertname"] == "MysteryAlert"
        assert "模拟分析结论" in ack.ai_advice[0]["advice"]

        # 记录进内存缓冲，可查最近记录
        assert len(agent.recent(10)) == 2


def test_aiops_assistant_process_alert_llm_unavailable_not_blocking(fakemonitor):
    """LLM 不可用（未注入）→ AI 分析跳过，告警记录与规则处置照常完成。"""
    class OkExecutor(ActionExecutor):
        name = "ok_action"
        description = "fake"
        def execute(self, params: dict) -> dict:
            return {"ok": True, "host": params["host"]}

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        rf = Path(td) / "rules.yaml"
        rf.write_text(
            "rules:\n  - alertname: HighCPUUsage\n    action: ok_action\n"
            "    target_instance: 192.168.1.10:9100\n"
            "    params: {host: h1}\n    auto_execute: true\n",
            encoding="utf-8",
        )
        engine = RuleEngine(executors={"ok_action": OkExecutor()}, rules_file=rf)
        agent = OpsAgent(monitor=fakemonitor, rule_engine=engine)  # 无 analysis/llm

        payload = make_payload(
            make_alert("HighCPUUsage"),
            make_alert("MysteryAlert"),
        )
        ack = agent.process_alert(payload)
        assert ack.received == 2
        assert len(ack.remediation) == 1
        assert ack.ai_advice == []   # LLM 不可用：不阻塞、无 AI 建议
        assert len(agent.recent()) == 2


def test_aiops_assistant_prints_remediation_to_console(fakemonitor, capsys):
    """处置结果的终端输出由 OpsAgent 负责（阶段 9 从 RuleEngine 移入）。

    文案逐字锁定：print 内容是用户观察 auto_execute=true 效果的主要窗口，
    搬动实现时最容易悄悄改掉的就是这里。
    """
    class OkExecutor(ActionExecutor):
        name = "ok_action"
        description = "fake"

        def execute(self, params: dict) -> dict:
            return {"ok": True, "host": params["host"]}

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        rf = Path(td) / "rules.yaml"
        rf.write_text(
            "rules:\n  - alertname: HighCPUUsage\n    action: ok_action\n"
            "    target_instance: 192.168.1.10:9100\n"
            "    params: {host: h1}\n    auto_execute: true\n",
            encoding="utf-8",
        )
        engine = RuleEngine(executors={"ok_action": OkExecutor()}, rules_file=rf)
        agent = OpsAgent(monitor=fakemonitor, rule_engine=engine)
        agent.process_alert(make_payload(make_alert("HighCPUUsage")))

    out = capsys.readouterr().out
    assert "[自动处置]" in out
    assert "alertname=HighCPUUsage" in out
    assert "status=ok" in out
    assert "action=ok_action" in out


def test_rule_engine_itself_prints_nothing(fakemonitor, capsys):
    """RuleEngine 不再自行输出：它只产出记录，输出归 OpsAgent。

    这是阶段 9「让 RuleEngine 变回纯决策、输出只剩一个口子」的护栏。
    """
    class OkExecutor(ActionExecutor):
        name = "ok_action"
        description = "fake"

        def execute(self, params: dict) -> dict:
            return {"ok": True}

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        rf = Path(td) / "rules.yaml"
        rf.write_text(
            "rules:\n  - alertname: HighCPUUsage\n    action: ok_action\n"
            "    target_instance: 192.168.1.10:9100\n"
            "    params: {}\n    auto_execute: true\n",
            encoding="utf-8",
        )
        engine = RuleEngine(executors={"ok_action": OkExecutor()}, rules_file=rf)
        records = engine.execute_for_payload(make_payload(make_alert("HighCPUUsage")))
        assert records and records[0]["status"] == "ok"

    assert capsys.readouterr().out == ""  # 引擎自身零输出


def test_aiops_assistant_active_alerts_propagates_monitor_error(fakemonitor):
    class BoomMonitor:
        def active_alerts(self):
            raise MonitorError("down")

    agent = OpsAgent(monitor=BoomMonitor())  # type: ignore[arg-type]
    import pytest as _pytest
    with _pytest.raises(MonitorError):
        agent.active_alerts()
