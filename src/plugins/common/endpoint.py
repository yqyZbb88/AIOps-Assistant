"""SSH 端点（Endpoint）统一抽象：连接参数与规约。

共享给三类命令边界：规则动作、Agent 写动作与只读诊断。多主机运行时连接目标统一
来自 `hosts.yaml` 登记的主机，命令目录只声明模板和业务参数。下方
`endpoint_from_params` 只保留旧单主机配置兼容路径，不应被新多主机 API 调用来接受
用户/模型提交的连接参数。

认证约定：key_filename（私钥）优先 → password；显式给了 key_filename 时不再试
agent / 默认密钥（由 ssh.py 的 exec_command 落实）。本模块顶层不 import paramiko。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: 未声明 `connect_timeout` 时的默认秒数。**本模块是它的唯一定义处。**
#:
#: 这个值同时被 ssh.py 用在**连接**（`connect`）和**每条命令**（`exec_command`）上，
#: 所以它是「连不上」与「命令跑不完」共用的上限。
#:
#: 取值与 config/diagnostics.yaml、config/agent_actions.yaml 里显式写着的 8 一致：
#: 同一套 SSH 层不该有两个「没写就用多少」的答案（这里曾是 15，而诊断与 Agent 写命令
#: 的配置模型默认 8——两条路径对同一个概念各留一个数，正是漂移的起点）。
DEFAULT_CONNECT_TIMEOUT: int = 8

#: 一条规则/命令 params 里表示「连接」的键名（其余键视为语义参数，进命令模板校验）
CONNECTION_FIELDS: tuple[str, ...] = (
    "host",
    "port",
    "username",
    "password",
    "key_filename",
    "known_hosts",
    "connect_timeout",
)


@dataclass(frozen=True)
class SshEndpoint:
    """连接一个 SSH 主机的参数集合。

    构造后留在执行器/诊断源实例内，永不进入模型上下文与工具参数。
    """

    host: str
    port: int = 22
    username: str = "root"
    key_filename: str | None = None
    known_hosts: str | None = None
    password: str | None = None
    connect_timeout: int = DEFAULT_CONNECT_TIMEOUT


def endpoint_from_params(
    params: Mapping[str, Any],
    *,
    default_timeout: int = DEFAULT_CONNECT_TIMEOUT,
) -> SshEndpoint:
    """从「规则 params」规约 SshEndpoint：host 必填，其余带默认；非法值抛 ValueError。

    仅用于旧单主机写动作兼容配置：连接字段在 rules.yaml 的 params 中声明，
    与语义参数混在一起；多主机路径必须改由登记主机的 SSH 目标提供。

    `connect_timeout` 未声明时用 `default_timeout`（= `DEFAULT_CONNECT_TIMEOUT`）。
    显式写 0 也落到默认值上——0 在 paramiko 里是「永不超时」，而一个不设上限的
    远程写命令会把整个处置流程挂死，那正是「没写」时不该发生的事。
    """
    host = params.get("host")
    if not host:
        raise ValueError("缺少必填连接参数 host")
    return SshEndpoint(
        host=str(host),
        port=int(params.get("port", 22) or 22),
        username=str(params.get("username") or "root"),
        key_filename=params.get("key_filename") or None,
        known_hosts=params.get("known_hosts") or None,
        password=params.get("password") or None,
        connect_timeout=int(params.get("connect_timeout", default_timeout) or default_timeout),
    )
