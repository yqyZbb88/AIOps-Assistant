"""只读诊断源契约（DiagnosticSource）。

给聊天 Agent 一条「在目标主机跑白名单只读命令」的能力，用于排查监控指标解释不了的
规则外问题（服务 failed 但没触发规则、CPU 高但找不到元凶进程、node-exporter 关机但想确认主机状态）。

安全边界（与动作执行互斥）：
- 凭据只存在于插件构造时注入的 config.target，**模型上下文里永无 host/密码/私钥**；
- 命令 = 配置文件声明的模板 + 已校验语义参数（见 plugins/diagnostics/commands.py），
  模型自由文本永不拼进 shell；
- 只读：sudo 恒 False，命令模板由人在 config/diagnostics.yaml 声明。

core / agent 层只依赖本接口；具体实现（SSH、将来的 Agent 直连等）在 plugins/diagnostics/。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

# 命令元信息（ParamSpec / CommandSpec）已迁至中性层 interfaces/command.py，
# 与写动作边界共用；此处 re-export 保持既有 import 路径不变。
from src.interfaces.command import CommandSpec, ParamSpec  # noqa: F401
from src.utils.freshness import freshness_note


class DiagnosticError(Exception):
    """只读诊断失败（未知命令 / 参数非法 / 连接与认证失败 / 执行异常）。"""


class DiagnosticSource(ABC):
    """只读诊断源：按命令名执行白名单只读命令，返回结果 dict。

    run() 语义：
    - 成功（无论远端退出码）：返回 {ok, host, command, exit_code, output}；
      只读命令非零退出码常见（grep 无匹配、systemctl 查 failed 服务返回 3），
      这类不算"调用失败"，ok=False 但输出照常回传，由 Agent 判断；
    - 未知命令 / 参数非法 / 连接认证失败 / 执行异常：抛 DiagnosticError，
      调用方（工具层）捕获后返回友好文本，不中断 Agent。
    """

    name: str = ""
    description: str = ""

    @abstractmethod
    def commands(self) -> tuple[CommandSpec, ...]:
        """返回当前可执行的命令清单（工具层据此枚举并建 Agent 工具）。"""

    @abstractmethod
    def run(self, command: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行白名单只读命令 command；params 只含语义参数，绝不含连接凭据。"""


def render_diag(result: dict[str, Any]) -> str:
    """把 run() 的结果 dict 渲染成给 Agent 的可读文本（纯函数）。

    三态：
    - 远端退出码 0 → "诊断结果（命令/exit_code）：\n<输出>"
    - 远端退出码非 0 → 开头标注非零退出码并附输出（由 Agent 判断是否异常）
    - 调用失败（error 字段）→ 直接给错误文本
    """
    host = result.get("host", "")
    command = result.get("command", "")
    error = result.get("error")
    if error:
        text = f"诊断命令执行失败{('（' + host + '）') if host else ''}：{error}"
        return text + "\n" + freshness_note(source="SSH 只读诊断", observed_at=result.get("executed_at"))

    output = result.get("output") or "(无输出)"
    exit_code = result.get("exit_code")
    if exit_code not in (0, None, ""):
        prefix = f"诊断命令已执行（host={host}）但远端返回非零退出码 {exit_code}，输出如下："
    else:
        prefix = f"诊断结果（host={host}，exit_code=0）："
    text = f"{prefix}\n```\n{output}\n```"
    return text + "\n" + freshness_note(source="SSH 只读诊断", observed_at=result.get("executed_at"))
