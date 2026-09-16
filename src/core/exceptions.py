"""业务异常基类。

core / 上层自定义业务异常统一继承 OpsAgentError，便于集中捕获；
基础设施异常（MonitorError / ActionError / LLMError）已在各自接口定义，
不重复在此定义。
"""
from __future__ import annotations


class OpsAgentError(Exception):
    """AIOps-Assistant 业务异常基类（结构位：当前无子类，预留）。"""
