"""日志工具：按 settings 控制级别，动作审计日志走独立 logger。

设计：
- 不做 logging.conf（搁置项）；用 config/settings.yaml 的 logging.level / audit 控制；
- 业务日志建议 `logging.getLogger("ops_agent")`（app name 前缀见 main.py 装配）；
- 动作审计走独立 logger `ops_agent.audit`，便于单独收集/过滤（见 plugins/actions）。
"""
from __future__ import annotations

import logging

# 审计日志专用 logger 名：动作执行器在其 execute 前后写入
AUDIT_LOGGER_NAME = "ops_agent.audit"

# 常规业务 logger 根名
APP_LOGGER_NAME = "ops_agent"

_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def configure_logging(level: str = "INFO", audit: bool = True) -> None:
    """配置根日志级别与审计通道（幂等；重复调用仅调整级别）。

    - level：根日志级别（DEBUG/INFO/WARNING/ERROR），basicConfig 未配置时才生效；
    - audit=False：把 ops_agent.audit 提到 WARNING，仅失败/异常进审计，静默成功执行。
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


def get_audit_logger() -> logging.Logger:
    """返回动作审计 logger（见 AUDIT_LOGGER_NAME）。"""
    return logging.getLogger(AUDIT_LOGGER_NAME)


def get_app_logger(name: str = APP_LOGGER_NAME) -> logging.Logger:
    """返回业务 logger，name 可追加子域（如 ops_agent.rule_engine）。"""
    return logging.getLogger(name)
