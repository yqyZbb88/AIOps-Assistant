"""storage：持久化/状态存储。

- models.py：跨层复用的数据模型（告警 webhook / 处置规则，pydantic 定义）；
- memory.py：最近告警的内存缓冲（AlertStore，进程内有效）；
- db.py：SQLite 持久化预留（搁置，仅 TODO）。
"""
