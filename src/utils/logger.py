"""日志工具：按 settings 控制级别，动作审计日志走独立 logger（可选落文件）。

设计：
- 不做 logging.conf（搁置项）；用 config/settings.yaml 的 logging.* 控制
  （main.create_app() 调用 configure_logging 装配根级别、审计通道与轮转文件）；
- 动作审计走独立 logger `ops_agent.audit`，便于单独收集/过滤（见 plugins/actions）；
- **审计日志可选落盘**：默认写 `logs/audit.log` 并轮转。在此之前审计只进 console，
  进程一重启就没了——而审计的用处恰恰是「事后追查」，重启即丢等于没有。
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

# 审计日志专用 logger 名：动作执行器在其 execute 前后写入
AUDIT_LOGGER_NAME = "ops_agent.audit"

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 打在文件 handler 上的标记属性：用于**幂等判断**（见 _attach_audit_file）
_FILE_HANDLER_TAG = "_ops_agent_audit_file"

DEFAULT_AUDIT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_AUDIT_BACKUP_COUNT = 3


def configure_logging(
    level: str = "INFO",
    audit: bool = True,
    *,
    audit_file: str = "",
    audit_max_bytes: int = DEFAULT_AUDIT_MAX_BYTES,
    audit_backup_count: int = DEFAULT_AUDIT_BACKUP_COUNT,
) -> None:
    """配置根日志级别、审计通道与审计文件（幂等；重复调用只调整级别/补挂文件）。

    - level：根日志级别（DEBUG/INFO/WARNING/ERROR），basicConfig 未配置时才生效；
    - audit=False：把 ops_agent.audit 提到 WARNING，仅失败/异常进审计，静默成功执行
      （**语义不变**：这不是「关闭审计」）；
    - audit_file：审计日志落盘路径（相对仓库根）；空串 = 只进 console，不落文件。
    """
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format=_FORMAT,
    )
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    if not audit:
        audit_logger.setLevel(logging.WARNING)
    else:
        audit_logger.setLevel(logging.NOTSET)  # 跟随根级别

    if audit_file:
        _attach_audit_file(
            audit_logger, audit_file,
            max_bytes=audit_max_bytes, backup_count=audit_backup_count,
        )


def _attach_audit_file(
    audit_logger: logging.Logger, path: str, *, max_bytes: int, backup_count: int
) -> None:
    """给审计 logger 挂一个轮转文件 handler（**幂等**）。

    三个必须处理的点：
    1. **幂等**：`configure_logging` 每次 `create_app` 都会被调用（测试里几十次），
       无条件 `addHandler` 会让每条审计写 N 遍，且 N 个 handler 争抢同一文件——
       Windows 上轮转 rename 会直接 PermissionError。故按路径查重。
    2. **建父目录**：`RotatingFileHandler` **不会**创建父目录，`logs/` 不存在时直接抛。
    3. **delay=True**：延迟到第一条日志才真正开文件，避免启动即占用句柄（便于删库/清理）。
    """
    target = str(path)
    for handler in audit_logger.handlers:
        if getattr(handler, _FILE_HANDLER_TAG, None) == target:
            return  # 已挂过，跳过

    from src.utils.config_loader import ROOT_DIR  # 同层依赖，避免顶层循环

    log_path = Path(path)
    if not log_path.is_absolute():
        log_path = ROOT_DIR / log_path
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 落盘失败不该拖垮服务：退化成只进 console
        logging.getLogger("ops_agent.logger").error(
            "无法创建审计日志目录 %s，审计仅输出到控制台: %s", log_path.parent, exc
        )
        return

    handler = RotatingFileHandler(
        log_path, maxBytes=max(int(max_bytes), 1), backupCount=max(int(backup_count), 0),
        encoding="utf-8", delay=True,
    )
    handler.setFormatter(logging.Formatter(_FORMAT))
    setattr(handler, _FILE_HANDLER_TAG, target)
    audit_logger.addHandler(handler)


def get_audit_logger() -> logging.Logger:
    """返回动作审计 logger（见 AUDIT_LOGGER_NAME）。"""
    return logging.getLogger(AUDIT_LOGGER_NAME)


def write_audit(record: dict[str, Any], *, level: int = logging.INFO) -> None:
    """把一条**结构化**审计记录写进 `ops_agent.audit`（一行一个 JSON 对象）。

    放在这里而不是 `plugins/common/audit.py`：审计是**跨层**的旁路通道，而两类写法
    （动作执行、人工审批决策）分处不同层——`api/` 不能 import `plugins/`，`core/`
    更是明令禁止。把「怎么写一条审计」收敛到 utils 这个叶子层，两边都能用，
    格式也不会漂。

    只负责序列化与投递，**不做打码**：调用方自己保证不落密码（动作那边过
    `redact_sensitive`）。失败只记 ERROR——审计写不进去不该让主流程炸掉。
    """
    try:
        get_audit_logger().log(level, "%s", json.dumps(record, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 序列化失败（如含不可序列化对象）
        logging.getLogger("ops_agent.logger").error("审计记录写入失败: %s", exc)
