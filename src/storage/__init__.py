"""storage：持久化/状态存储。

- models.py：跨层复用的数据模型（告警 webhook / 处置规则，pydantic 定义）；
- memory.py：最近告警的内存缓冲（AlertStore，进程内有效）；
- db.py：SQLite 连接、schema 迁移与版本校验；
- session.py / history.py / inspection.py：会话、告警归档及巡检异常；
- approval.py：进程内待审批队列；approval_history.py：独立持久申请和历史查询；
- execution.py / remediation_guard.py：统一执行审计与规则幂等抢占。
"""
