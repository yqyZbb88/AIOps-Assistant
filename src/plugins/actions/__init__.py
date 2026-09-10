"""写命令执行器插件包（WRITE 边界）。

M1b 起动作不再按「一个动作一个 Python 类」注册进 registry，改为：
- config/actions.yaml 声明**允许执行的写命令目录**（模板 + 参数 schema + sudo）；
- catalog.py 加载目录 → executor.py 的 CatalogCommandExecutor 按 key 通用执行；
- 规则（rules.yaml）只引用目录 key 并提供语义参数，只能从目录里选命令执行。

本包 import 无副作用、不向 registry 注册（生产构造在 container.executors() 惰性按目录构建），
顶层不 import paramiko / langchain。
"""
from src.plugins.actions import catalog, executor  # noqa: F401

__all__ = ["catalog", "executor"]
