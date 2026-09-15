"""storage：持久化/状态存储。

- models.py：跨层复用的数据模型（告警 webhook / 处置规则，pydantic 定义）；
- memory.py：最近告警的内存缓冲（AlertStore，进程内有效）；
- db.py：SQLite 连接、schema 迁移与版本校验；
- session.py / history.py / approval.py / remediation_guard.py / inspection.py：会话、归档、审批、幂等抢占和巡检异常归档。
"""
