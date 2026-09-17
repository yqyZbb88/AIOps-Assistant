"""日志工具：按 settings 控制级别，动作审计日志走独立 logger（可选落文件）。

设计：
- 不做 logging.conf（搁置项）；用 config/settings.yaml 的 logging.* 控制
  （main.create_app() 调用 configure_logging 装配根级别、审计通道与轮转文件）；
- 动作审计走独立 logger `aiops_assistant.audit`，便于单独收集/过滤（见 plugins/actions）；
- **审计日志可选落盘**：默认写 `logs/audit.log` 并轮转。在此之前审计只进 console，
  进程一重启就没了——而审计的用处恰恰是「事后追查」，重启即丢等于没有。
"""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from src.utils.security import redact_sensitive, redact_text

# 审计日志专用 logger 名：动作执行器在其 execute 前后写入
AUDIT_LOGGER_NAME = "aiops_assistant.audit"
# 固定指标巡检专用 logger：每轮写一条汇总，并可选落到独立轮转文件。
INSPECTION_LOGGER_NAME = "aiops_assistant.inspection"

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 打在文件 handler 上的标记属性：用于**幂等判断**（见 _attach_audit_file）
_FILE_HANDLER_TAG = "_aiops_assistant_audit_file"
_INSPECTION_FILE_HANDLER_TAG = "_aiops_assistant_inspection_file"

DEFAULT_AUDIT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_AUDIT_BACKUP_COUNT = 3


class RedactingFormatter(logging.Formatter):
    """在写入目的地前清理最终渲染文本（包括异常 traceback）。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


class RedactingFilter(logging.Filter):
    """清理 LogRecord，使第三方 handler 也不会收到常见明文凭据。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive(record.msg)
        if isinstance(record.args, dict):
            record.args = redact_sensitive(record.args)
        elif isinstance(record.args, tuple):
            record.args = tuple(redact_sensitive(value) for value in record.args)
        record.exc_text = None
        return True


def _secure_handler(handler: logging.Handler) -> None:
    if not any(isinstance(item, RedactingFilter) for item in handler.filters):
        handler.addFilter(RedactingFilter())
    handler.setFormatter(RedactingFormatter(_FORMAT))


def configure_logging(
    level: str = "INFO",
    audit: bool = True,
    *,
    audit_file: str = "",
    audit_max_bytes: int = DEFAULT_AUDIT_MAX_BYTES,
    audit_backup_count: int = DEFAULT_AUDIT_BACKUP_COUNT,
    inspection_file: str = "",
    inspection_max_bytes: int = DEFAULT_AUDIT_MAX_BYTES,
    inspection_backup_count: int = DEFAULT_AUDIT_BACKUP_COUNT,
) -> None:
    """配置根日志级别、审计通道与审计文件（幂等；重复调用只调整级别/补挂文件）。

    - level：根日志级别（DEBUG/INFO/WARNING/ERROR），basicConfig 未配置时才生效；
    - audit=False：把 aiops_assistant.audit 提到 WARNING，仅失败/异常进审计，静默成功执行
      （**语义不变**：这不是「关闭审计」）；
    - audit_file：审计日志落盘路径（相对仓库根）；空串 = 只进 console，不落文件。
    - inspection_file：巡检全量汇总日志路径；空串 = 只进 console。
    """
    logging.basicConfig(
        level=getattr(logging, str(level).upper(), logging.INFO),
        format=_FORMAT,
    )
    for handler in logging.getLogger().handlers:
        _secure_handler(handler)
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

    inspection_logger = logging.getLogger(INSPECTION_LOGGER_NAME)
    inspection_logger.setLevel(logging.NOTSET)  # 跟随根级别
    # 巡检只进入专用文件，不再传播到 root console。若未配置文件，日志会被丢弃。
    inspection_logger.propagate = False
    if inspection_file:
        _attach_inspection_file(
            inspection_logger,
            inspection_file,
            max_bytes=inspection_max_bytes,
            backup_count=inspection_backup_count,
        )


def _attach_rotating_file(
    target_logger: logging.Logger,
    path: str,
    *,
    max_bytes: int,
    backup_count: int,
    tag: str,
    description: str,
) -> None:
    """给指定 logger 幂等挂一个轮转文件 handler。"""
    target = str(path)
    for handler in target_logger.handlers:
        if getattr(handler, tag, None) == target:
            return

    from src.utils.config_loader import ROOT_DIR  # 同层依赖，避免顶层循环

    log_path = Path(path)
    if not log_path.is_absolute():
        log_path = ROOT_DIR / log_path
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logging.getLogger("aiops_assistant.logger").error(
            "无法创建%s目录 %s，仅输出到控制台: %s", description, log_path.parent, exc
        )
        return

    handler = RotatingFileHandler(
        log_path,
        maxBytes=max(int(max_bytes), 1),
        backupCount=max(int(backup_count), 0),
        encoding="utf-8",
        delay=True,
    )
    _secure_handler(handler)
    setattr(handler, tag, target)
    target_logger.addHandler(handler)


def _attach_audit_file(
    audit_logger: logging.Logger, path: str, *, max_bytes: int, backup_count: int
) -> None:
    """给审计 logger 挂文件 handler（幂等）。"""
    _attach_rotating_file(
        audit_logger,
        path,
        max_bytes=max_bytes,
        backup_count=backup_count,
        tag=_FILE_HANDLER_TAG,
        description="审计日志",
    )


def _attach_inspection_file(
    inspection_logger: logging.Logger,
    path: str,
    *,
    max_bytes: int,
    backup_count: int,
) -> None:
    """给巡检 logger 挂独立轮转文件 handler（幂等）。"""
    _attach_rotating_file(
        inspection_logger,
        path,
        max_bytes=max_bytes,
        backup_count=backup_count,
        tag=_INSPECTION_FILE_HANDLER_TAG,
        description="巡检日志",
    )


def get_audit_logger() -> logging.Logger:
    """返回动作审计 logger（见 AUDIT_LOGGER_NAME）。"""
    return logging.getLogger(AUDIT_LOGGER_NAME)


def get_inspection_logger() -> logging.Logger:
    """返回固定指标巡检 logger（见 INSPECTION_LOGGER_NAME）。"""
    return logging.getLogger(INSPECTION_LOGGER_NAME)


def write_audit(record: dict[str, Any], *, level: int = logging.INFO) -> None:
    """把一条**结构化**审计记录写进 `aiops_assistant.audit`（一行一个 JSON 对象）。

    放在这里而不是 `plugins/common/audit.py`：审计是**跨层**的旁路通道，而两类写法
    （动作执行、人工审批决策）分处不同层——`api/` 不能 import `plugins/`，`core/`
    更是明令禁止。把「怎么写一条审计」收敛到 utils 这个叶子层，两边都能用，
    格式也不会漂。

    只负责序列化与投递，**不做打码**：调用方自己保证不落密码（动作那边过
    `redact_sensitive`）。失败只记 ERROR——审计写不进去不该让主流程炸掉。
    """
    try:
        get_audit_logger().log(
            level, "%s", json.dumps(redact_sensitive(record), ensure_ascii=False)
        )
    except Exception as exc:  # noqa: BLE001 - 序列化失败（如含不可序列化对象）
        logging.getLogger("aiops_assistant.logger").error("审计记录写入失败: %s", exc)
