"""测试公共：FakeMonitor / FakeLLM / 告警构造工具 + 确保仓库根在 sys.path。"""
from __future__ import annotations

import sys
from pathlib import Path

# LangSmith 追踪由测试进程的环境变量控制。完全离线运行时应将
# LANGSMITH_TRACING、LANGCHAIN_TRACING 和 LANGCHAIN_TRACING_V2 设为 false。

import pytest  # noqa: E402

# 保证在任意 pytest 调用方式下都能 `import src`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 堵住仓库里那个**真实**数据库：测试绝不允许碰它
# ---------------------------------------------------------------------------
# 为什么需要：`import src.main` 会执行模块级的 `app = create_app()`，而它用**仓库真实配置**
# （config/settings.yaml 的 storage.enabled: true）装配容器 → `container.sessions()` →
# `open_database(ROOT_DIR/"data/aiops_assistant.db")`。**开库会跑迁移、也会写数据**，
# 于是跑一次测试就可能改动——甚至迁移坏——用户的真实库。实测确认过：
# 只 `import src.main` 就能让那个文件的 mtime 变化。
#
# **必须写在模块级、不能写成 fixture**：`import src.main` 发生在 pytest 的**收集阶段**
# （导入各测试模块时），比任何 fixture 都早——fixture 挂上去时库已经被打开了。
#
# 做法：把 `open_database` 换成「只接受仓库外路径」的版本。测试自己用 tmp_path 开库照常，
# 唯独指向仓库 data/ 的那次拿到 None —— 正是「持久化不可用」的既有语义（降级、服务照常）。
def _block_real_database() -> None:
    import src.storage.db as dbmod

    real_open = dbmod.open_database
    data_dir = (ROOT / "data").resolve()

    def guarded(path, *args, **kwargs):
        try:
            if Path(path).resolve().is_relative_to(data_dir):
                return None
        except Exception:  # noqa: BLE001 - 路径怪到解析不了，交给原实现去报错
            pass
        return real_open(path, *args, **kwargs)

    dbmod.open_database = guarded


_block_real_database()

from src.interfaces.agent_action import AgentActionError, AgentActionSource  # noqa: E402
from src.interfaces.diagnostic import (
    CommandSpec,
    DiagnosticError,
    DiagnosticSource,
    ParamSpec,
)  # noqa: E402
from src.interfaces.llm_client import LLMClient  # noqa: E402
from src.interfaces.metric import MetricSample  # noqa: E402
from src.interfaces.monitor import MonitorError, MonitorSource  # noqa: E402
from src.storage.models import Alert, AlertmanagerPayload  # noqa: E402

# 单指标"有数据"的趋势汇总
_TREND_OK = {"latest": 85.0, "min": 60.0, "max": 90.0, "avg": 75.0, "points": 12}
# 单指标"无数据"的趋势汇总
_TREND_EMPTY = {"latest": None, "min": None, "max": None, "avg": None, "points": 0}


class FakeMonitor(MonitorSource):
    """可控的监控源：注入固定数值 / 告警，验证 core 与聊天兜底逻辑。"""

    def __init__(
        self,
        cpu: float | None = 45.0,
        memory: float | None = 60.0,
        disks: list[dict] | None = None,
        alerts: list[dict] | None = None,
        *,
        raise_cpu: bool = False,
        metric_samples: dict[str, list[MetricSample]] | None = None,
    ) -> None:
        self._cpu = cpu
        self._memory = memory
        self._disks = disks if disks is not None else []
        self._alerts = alerts if alerts is not None else []
        self._raise_cpu = raise_cpu
        self._metric_samples = metric_samples or {}

    def cpu_usage(self) -> float | None:
        if self._raise_cpu:
            raise MonitorError("Prometheus 连接失败（模拟）")
        return self._cpu

    def memory_usage(self) -> float | None:
        return self._memory

    def disk_usage(self) -> list[dict]:
        return list(self._disks)

    def active_alerts(self) -> list[dict]:
        return list(self._alerts)

    def query_metric(self, metric_id: str, *, selector=None) -> list[MetricSample]:
        if metric_id in self._metric_samples:
            return list(self._metric_samples[metric_id])
        if metric_id == "cpu.usage":
            if self._raise_cpu:
                raise MonitorError("Prometheus 连接失败（模拟）")
            if self._cpu is None:
                return []
            return [MetricSample(metric_id, {"instance": "h:9100"}, self._cpu)]
        if metric_id == "memory.usage":
            if self._memory is None:
                return []
            return [MetricSample(metric_id, {"instance": "h:9100"}, self._memory)]
        if metric_id == "disk.usage":
            return [
                MetricSample(
                    metric_id,
                    {"mountpoint": str(item.get("mountpoint", "unknown"))},
                    float(item["usage"]),
                )
                for item in self._disks
            ]
        if metric_id == "alerts.active_count":
            visible = [a for a in self._alerts if a.get("state") in ("pending", "firing")]
            return [
                MetricSample(
                    metric_id,
                    {"state": state},
                    float(sum(1 for a in visible if a.get("state") == state)),
                )
                for state in ("firing", "pending")
            ]
        raise MonitorError(f"FakeMonitor 未配置指标: {metric_id}")

    def query_metric_trend(
        self, metric_id: str, instance: str, *, minutes: int = 60, step: str = "5m", selector=None
    ) -> dict:
        if metric_id == "cpu.usage":
            return dict(_TREND_OK)
        if metric_id == "memory.usage":
            return dict(_TREND_EMPTY)
        if metric_id == "disk.usage":
            return dict(_TREND_OK)
        if metric_id in self._metric_samples and self._metric_samples[metric_id]:
            value = self._metric_samples[metric_id][0].value
            return {"latest": value, "min": value, "max": value, "avg": value, "points": 1}
        raise MonitorError(f"FakeMonitor 未配置趋势: {metric_id}")

    def instance_trend(
        self, instance: str, *, minutes: int = 60, step: str = "5m"
    ) -> dict[str, dict]:
        # CPU 有数据，内存无数据（模拟实例未采集某项指标）
        return {
            "CPU使用率": dict(_TREND_OK),
            "内存使用率": dict(_TREND_EMPTY),
            "磁盘使用率": dict(_TREND_OK),
        }


class FakeLLM(LLMClient):
    """可控的 LLM 客户端：返回固定文本，记录收到的 prompt。"""

    name = "fake"

    def __init__(self, text: str = "建议：先查磁盘与日志（模拟分析结论）") -> None:
        self.text = text
        self.calls: list[tuple[str, str | None]] = []

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.calls.append((prompt, system))
        return self.text


class FakeDiagnostics(DiagnosticSource):
    """可控的只读诊断源：按命令名返回固定输出，可指定命令抛 DiagnosticError。

    records: {command key: 输出文本}；fail: 一组命令名（调用即抛模拟失败）；
    specs: 可注入命令清单，缺省用 `memory`(无参) + `service_status(service)`。
    """

    name = "fake_diag"
    description = "模拟只读诊断源"

    DEFAULT_SPECS: tuple[CommandSpec, ...] = (
        CommandSpec(key="memory", summary="查看内存（模拟）"),
        CommandSpec(
            key="service_status",
            summary="查看服务状态（模拟）",
            params=(ParamSpec(name="service", type="str", required=True),),
        ),
    )

    def __init__(
        self,
        records: dict[str, str] | None = None,
        *,
        specs: tuple[CommandSpec, ...] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self._records = records or {}
        self._specs = specs if specs is not None else self.DEFAULT_SPECS
        self._fail = set(fail or [])
        self.calls: list[tuple[str, dict]] = []

    def commands(self) -> tuple[CommandSpec, ...]:
        return tuple(self._specs)

    def run(self, command: str, *, params: dict | None = None) -> dict:
        self.calls.append((command, dict(params or {})))
        if command in self._fail:
            raise DiagnosticError(f"{command} 模拟失败")
        if command not in self._records:
            raise DiagnosticError(f"未知诊断命令 {command!r}")
        return {
            "ok": True,
            "host": "fake",
            "command": command,
            "exit_code": 0,
            "output": self._records[command],
        }


class FakeAgentActions(AgentActionSource):
    """可控的 Agent 写命令源：按命令名记录调用并返回固定结果。

    perform: 一组「执行时抛 AgentActionError」的命令名（模拟远端失败）；
    真实实现会真的改系统——这里只记录调用，用于断言「批准前是否被调用」。
    """

    name = "fake_agent_actions"
    description = "模拟 Agent 写命令源"

    DEFAULT_SPECS: tuple[CommandSpec, ...] = (
        CommandSpec(
            key="restart_service",
            summary="重启服务（模拟）",
            params=(ParamSpec(name="service", type="str", required=True),),
        ),
    )

    def __init__(
        self,
        *,
        specs: tuple[CommandSpec, ...] | None = None,
        fail: set[str] | None = None,
    ) -> None:
        self._specs = specs if specs is not None else self.DEFAULT_SPECS
        self._fail = set(fail or [])
        self.calls: list[tuple[str, dict]] = []

    def commands(self) -> tuple[CommandSpec, ...]:
        return tuple(self._specs)

    def run(self, command: str, *, params: dict | None = None) -> dict:
        self.calls.append((command, dict(params or {})))
        if command in self._fail:
            raise AgentActionError(f"{command} 模拟失败")
        return {
            "ok": True,
            "host": "fake",
            "command": command,
            "exit_code": 0,
            "output": f"{command} 已执行（模拟）",
        }


# ---------------------------------------------------------------------------
# 工具：构造 Alert / Payload
# ---------------------------------------------------------------------------
def make_alert(
    name: str,
    *,
    status: str = "firing",
    instance: str = "192.168.1.10:9100",
    severity: str = "critical",
    summary: str = "",
) -> Alert:
    return Alert(
        status=status,
        labels={"alertname": name, "severity": severity, "instance": instance},
        annotations={"summary": summary},
        startsAt="2026-01-01T00:00:00Z",
    )


def make_payload(*alerts: Alert) -> AlertmanagerPayload:
    return AlertmanagerPayload(status="firing", alerts=list(alerts))


@pytest.fixture
def fakemonitor() -> FakeMonitor:
    return FakeMonitor()


@pytest.fixture
def fakellm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fakediag() -> FakeDiagnostics:
    return FakeDiagnostics(records={"memory": "total 1024  used 512  free 512"})


@pytest.fixture
def fake_actions() -> FakeAgentActions:
    return FakeAgentActions()


# ---------------------------------------------------------------------------
# 全局隔离：测试不得往仓库里写审计日志
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _no_audit_file_in_tests():
    """摘掉审计 logger 上的文件 handler，让测试套件绝不往仓库写文件。

    为什么需要：`import src.main` 会执行模块级的 `app = create_app()`，而它用
    **仓库真实配置**（config/settings.yaml 的 logging.audit_file）给 audit logger 挂上
    `logs/audit.log` 的文件 handler。此后任何一条审计（测试里被 stub 掉的
    exec_command 也会走 record_execution）都会写进仓库——测试跑完在仓库里留下
    一堆假数据（192.168.1.1 / h / boom 这类）。

    文件落盘本身由 tests/test_logger.py 用 tmp_path 专门覆盖，不需要真实路径参与。
    """
    import logging

    from src.utils.logger import (
        _FILE_HANDLER_TAG,
        _INSPECTION_FILE_HANDLER_TAG,
        AUDIT_LOGGER_NAME,
        INSPECTION_LOGGER_NAME,
    )

    for logger_name, tag in (
        (AUDIT_LOGGER_NAME, _FILE_HANDLER_TAG),
        (INSPECTION_LOGGER_NAME, _INSPECTION_FILE_HANDLER_TAG),
    ):
        target = logging.getLogger(logger_name)
        for handler in list(target.handlers):
            if getattr(handler, tag, None):
                target.removeHandler(handler)
    yield


# ---------------------------------------------------------------------------
# 全局隔离：测试绝不允许加载**真实模型**（那会发起真实 DeepSeek 调用）
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _no_real_model_in_tests(monkeypatch):
    """把模型入口换成「一调用就炸」，任何漏注入假模型的用例立刻失败。

    为什么需要：聊天 Agent 的构建**没有任何开关**——`llm.enabled=false` 只让容器跳过
    告警 AI 分析，聊天走的 `container._assistant_factory()` → `OpsAssistant` →
    `utils/model_loader.get_model()` → 根 `init.py` 的 `init_chat_model`，配了
    `.env` 的 key 就**真的会发请求**。而失败形态是**成功**（返回一段看起来正常的回答），
    不是报错——所以只能靠这道闸门拦。

    注意必须在**两个**地方打补丁：`model_loader.get_model` 是对模块的调用；
    而 `agent/assistant.py` 在**模块顶层**做了 `from ... import get_model`，
    已导入的话得打它自己那份引用。测试若自己 `monkeypatch.setattr(assistant_mod,
    "get_model", ...)` 注入脚本化模型，会覆盖本补丁（后打的赢）——那正是期望的用法。
    """
    import sys

    def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "测试里不得加载真实模型——这会发起真实 API 调用。"
            "请照 tests/test_agent_actions_hitl_graph.py 注入脚本化模型，"
            "或给 ChatService._assistant 注入假对象（如 FakeHistoryAssistant）。"
        )

    import src.utils.model_loader as model_loader

    monkeypatch.setattr(model_loader, "get_model", _forbidden)
    assistant_mod = sys.modules.get("src.agent.assistant")
    if assistant_mod is not None:
        monkeypatch.setattr(assistant_mod, "get_model", _forbidden, raising=False)

# ---------------------------------------------------------------------------
# 全局隔离：测试绝不允许建立**对外**网络连接
# ---------------------------------------------------------------------------
_VIOLATIONS: list[str] = []


@pytest.fixture(scope="session", autouse=True)
def _no_external_network_in_tests():
    """任何对**非回环地址**的连接一律失败。硬性约束：测试全部离线。

    比「给某个具体客户端打补丁」彻底得多——它不关心是哪个库、哪条线程发的请求。
    这道闸门是为一个**实测踩到**的坑加的：用户 `.env` 里配了 LangSmith 时，langchain
    会起一个后台线程周期性往 api.smith.langchain.com 发 `client.info`，**与有没有真跑模型
    无关**。它绕过了一切模型层的桩（`get_model` 打得再死也没用），而且测试**照样全绿**——
    只有抓 socket 才看得见。

    回环地址放行（本机服务、asyncio 的 socketpair 等）；`address` 不是元组的一律放行
    （socketpair 的地址是整数，不是 (host, port)）。
    """
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(address) -> None:
        if not isinstance(address, tuple):
            return  # 本机 socketpair 之类，不走网络
        host = str(address[0])
        if host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", ""):
            return
        message = (
            f"测试里出现了对外的网络连接：{address}。本仓库要求测试**全部离线**。"
            "请把对应客户端换成本地桩；若它是某个库的后台线程（如 LangSmith 追踪），"
            "在 conftest 顶部把它关掉。"
        )
        _VIOLATIONS.append(message)
        raise AssertionError(message)

    def spy(self, address, *a, **k):
        _check(address)
        return real_connect(self, address, *a, **k)

    def spy_ex(self, address, *a, **k):
        _check(address)
        return real_connect_ex(self, address, *a, **k)

    socket.socket.connect = spy
    socket.socket.connect_ex = spy_ex
    yield
    socket.socket.connect = real_connect
    socket.socket.connect_ex = real_connect_ex


def pytest_sessionfinish(session, exitstatus) -> None:
    """后台线程里的违规会被它自己吞掉，这里兜底把整轮判为失败。"""
    if _VIOLATIONS:
        print()
        print("=" * 60)
        print("测试出现了对外网络连接（本仓库要求测试全部离线）：")
        for item in _VIOLATIONS:
            print("   ", item)
        print("=" * 60)
        session.exitstatus = 1
