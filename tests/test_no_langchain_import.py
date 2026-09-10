"""验证：未装 langchain / python-dotenv 时核心模块可 import、聊天照常回落。

做法：在全新子解释器里先把 langchain/langchain_core/langgraph/langchain_deepseek/dotenv
用 sys.modules[name]=None 屏蔽（等价"未安装"，任何 import 尝试都会报 ImportError），
再 import container / core / plugins / storage / utils 并跑 OpsAgent 聊天回落。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_SCRIPT = r"""
import sys
for _name in ("langchain", "langchain_core", "langgraph",
              "langchain_deepseek", "dotenv"):
    sys.modules[_name] = None  # 屏蔽：任何 import 尝试都报 ImportError

sys.path.insert(0, __ROOT_REPR__)

# 1) 全链路 import（不应触发 langchain / dotenv）
from src.container import Container          # noqa: F401
from src.core.agent import OpsAgent          # noqa: F401
from src.core.analysis import AlertAnalyzer  # noqa: F401
from src.core.chat import ChatService        # noqa: F401
from src.core.rule_engine import RuleEngine  # noqa: F401
from src.storage.memory import AlertStore    # noqa: F401
from src.storage.approval import ApprovalStore  # noqa: F401
from src.storage.models import Alert, AlertmanagerPayload, ActionRule  # noqa: F401
from src.utils import security, config_loader, logger  # noqa: F401
from src.plugins.monitors import prometheus_monitor  # noqa: F401
from src.plugins.actions import catalog, executor  # noqa: F401
# M3：Agent 写命令族与审批契约也必须能在无 langchain 环境导入
from src.interfaces.agent_action import AgentActionError, AgentActionSource  # noqa: F401
from src.interfaces.approval import ApprovalError, PendingApproval  # noqa: F401
from src.plugins.agent_actions import catalog as agent_actions_catalog  # noqa: F401
from src.plugins.agent_actions import source as agent_actions_source  # noqa: F401

# 2) FakeMonitor + 聊天回落断言
class FakeMonitor:
    def cpu_usage(self):
        return 12.3
    def memory_usage(self):
        return None
    def disk_usage(self):
        return []
    def active_alerts(self):
        return []
    def instance_trend(self, instance, *, minutes=60, step="5m"):
        return {}

agent = OpsAgent(monitor=FakeMonitor())
assert "CPU 使用率" in agent.chat("CPU"), agent.chat("CPU")
assert agent.chat("你好") == "我不理解，请尝试其他问题"

# 3) container 也能构造（settings 走默认，不触发 langchain）
c = Container()
assert c.settings.app.name

print("OK: no-langchain environment fully works")
"""


def test_no_langchain_environment_still_works():
    script = _SCRIPT.replace("__ROOT_REPR__", repr(str(ROOT)))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, f"子进程失败 rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    assert "OK: no-langchain" in proc.stdout
