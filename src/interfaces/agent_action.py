"""Agent 写命令源契约（AgentActionSource）。

给聊天 Agent 一条「会改动目标系统」的写命令通道，与只读诊断（DiagnosticSource）
边界严格分离：
- 只读诊断：恒不 sudo、远端非零退出码**不算失败**、可自动调用（低危）；
- 本契约（写命令）：sudo 由管理员逐条声明、远端非零退出码**就是失败**、
  且**每次调用都必须经人工审批**后才会真正执行（高危）。

安全边界：
- 凭据只存在于插件构造时注入的配置（config/agent_actions.yaml 的 target），
  **模型上下文与工具参数里永无 host/密码/私钥**；
- 命令 = 配置声明的模板 + 已校验语义参数（见 plugins/common/cmddef.py），
  模型自由文本永不拼进 shell；
- 审批不在此层实现：本层只管「按命令名执行」，审批由 Agent 编排层
  （agent/assistant.py 的 HumanInTheLoopMiddleware）与 api/approvals.py 强制前置。
  即：本层的 run() 一旦被调用就真的会执行——**审批必须在调用它之前完成**。

core / api 层只依赖本接口与下面的中性类型；具体实现（SSH 等）在 plugins/agent_actions/。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from src.interfaces.command import CommandSpec  # noqa: F401  （re-export 便于使用方一处引入）
from src.utils.freshness import freshness_note


class AgentActionError(Exception):
    """Agent 写命令失败（未知命令 / 参数非法 / 连接与认证失败 / 远端非零退出码）。

    注意语义与只读诊断相反：**远端非零退出码在这里就是失败**（写动作没做成），
    而只读诊断把非零退出码当正常返回（grep 无匹配、systemctl 查 failed 返回 3）。
    """


class AgentActionSource(ABC):
    """Agent 写命令源：按命令名执行白名单写命令，返回结果 dict。

    run() 语义（写边界）：
    - 成功：返回 {ok: True, host, command, exit_code, output}；
    - 未知命令 / 参数非法（含越权携带连接字段）/ 连接认证失败 / 远端非零退出码：
      一律抛 AgentActionError，调用方（工具层 / 续跑路径）捕获后转可读文本；
    - params 只含语义参数，**绝不含** host/port/username/password 等连接字段。

    实现方必须保证：每次真实 SSH 尝试都写审计（plugins/common/audit）。
    """

    name: str = ""
    description: str = ""

    @abstractmethod
    def commands(self) -> tuple[CommandSpec, ...]:
        """返回当前可执行的写命令清单（工具层据此枚举并建 Agent 工具 + 审批策略）。"""

    @abstractmethod
    def run(self, command: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行白名单写命令 command；params 只含语义参数，绝不含连接凭据。"""


def render_agent_action(result: dict[str, Any]) -> str:
    """把 run() 的成功结果渲染成给 Agent 的可读文本（纯函数）。

    写语义：能走到这里就是成功（失败已在 run() 里抛 AgentActionError），
    因此不区分退出码，直接给「已执行」+ 输出。

    刻意不复用 render_diag：后者的措辞是只读语义（「诊断结果」/「远端返回非零退出码」）
    且被测试逐字断言，写边界需要不同的文案。
    """
    # 这段文本会进入外部模型上下文，只使用服务端主机身份/脱敏摘要，
    # 不把 SSH 连接地址回传给模型。
    host = (
        result.get("target_summary")
        or result.get("host_id")
        or "(服务端绑定目标)"
    )
    command = result.get("command", "")
    output = result.get("output") or "(无输出)"
    text = f"写命令已执行（host={host}，command={command}）：\n```\n{output}\n```"
    return text + "\n" + freshness_note(source="SSH 写命令执行", observed_at=result.get("executed_at"))
