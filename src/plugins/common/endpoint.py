"""SSH 端点（Endpoint）统一抽象：连接参数与规约。

共享给两条边界：
- actions（写动作）：端点来自 config/rules.yaml 每条规则的 params（host/port/username/…）；
- diagnostics（只读诊断）：端点来自 config/diagnostics.yaml 的 target。

认证约定：key_filename（私钥）优先 → password；显式给了 key_filename 时不再试
agent / 默认密钥（由 ssh.py 的 exec_command 落实）。本模块顶层不 import paramiko。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

#: 一条规则/命令 params 里表示「连接」的键名（其余键视为语义参数，进命令模板校验）
CONNECTION_FIELDS: tuple[str, ...] = (
    "host",
    "port",
    "username",
    "password",
    "key_filename",
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
    password: str | None = None
    connect_timeout: int = 15


def endpoint_from_params(
    params: Mapping[str, Any],
    *,
    default_timeout: int = 15,
) -> SshEndpoint:
    """从「规则 params」规约 SshEndpoint：host 必填，其余带默认；非法值抛 ValueError。

    用于写动作：连接字段由管理员在 rules.yaml 声明（密码以 .env 占位插值），
    与语义参数混在同一条 params 里，这里只取连接字段。
    """
    host = params.get("host")
    if not host:
        raise ValueError("缺少必填连接参数 host")
    return SshEndpoint(
        host=str(host),
        port=int(params.get("port", 22) or 22),
        username=str(params.get("username") or "root"),
        key_filename=params.get("key_filename") or None,
        password=params.get("password") or None,
        connect_timeout=int(params.get("connect_timeout", default_timeout) or default_timeout),
    )
