"""测试公共：FakeMonitor / FakeLLM / 告警构造工具 + 确保仓库根在 sys.path。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 保证在任意 pytest 调用方式下都能 `import src`
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.interfaces.agent_action import AgentActionError, AgentActionSource  # noqa: E402
from src.interfaces.diagnostic import (
    CommandSpec,
    DiagnosticError,
    DiagnosticSource,
    ParamSpec,
)  # noqa: E402
from src.interfaces.llm_client import LLMClient  # noqa: E402
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
    ) -> None:
        self._cpu = cpu
        self._memory = memory
        self._disks = disks if disks is not None else []
        self._alerts = alerts if alerts is not None else []
        self._raise_cpu = raise_cpu

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

    from src.utils.logger import _FILE_HANDLER_TAG, AUDIT_LOGGER_NAME

    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    for handler in list(audit_logger.handlers):
        if getattr(handler, _FILE_HANDLER_TAG, None):
            audit_logger.removeHandler(handler)
    yield
