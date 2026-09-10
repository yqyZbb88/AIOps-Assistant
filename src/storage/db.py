"""SQLite 持久化（搁置项，仅预留）。

TODO(后续阶段)：把 AlertStore 的内存缓冲落盘，支持服务重启后仍能查询历史告警。
当前内存实现见 src/storage/memory.py（进程内有效，重启清空）。

预留的演进方向：
    - 告警记录表（webhook 摘要，主键 fingerprint/startsAt）
    - 处置/审计结果归档（rule_engine + actions 输出，便于事后审计）
    - 依赖 SQLAlchemy 或标准库 sqlite3，由 settings.yaml 控制开关与文件路径
本文件当前无实现，仅保留占位，避免未来改动面过大。
"""
