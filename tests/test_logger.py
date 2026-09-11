"""审计日志落盘：幂等、建父目录、开关语义（全离线，写 tmp_path）。"""
from __future__ import annotations

import logging

import pytest

from src.utils.logger import _FILE_HANDLER_TAG, AUDIT_LOGGER_NAME, configure_logging


@pytest.fixture(autouse=True)
def _isolate_audit_handlers():
    """logger 的 handler 是**全局状态**，必须逐用例隔离。

    关键：`import src.main` 会执行模块级的 `app = create_app()`，用**仓库真实配置**
    给 audit logger 挂上一个文件 handler。它会跨用例残留，让「只剩自己挂的那一个」
    这类断言全部失效（本文件单独跑能过、全量跑就红，就是它在作祟）。
    故用例前摘掉所有文件 handler，用例后恢复原状。
    """
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
    saved = list(audit_logger.handlers)
    for handler in saved:
        if getattr(handler, _FILE_HANDLER_TAG, None):
            audit_logger.removeHandler(handler)
    yield
    for handler in list(audit_logger.handlers):
        audit_logger.removeHandler(handler)
        if handler not in saved:
            handler.close()
    for handler in saved:
        audit_logger.addHandler(handler)


def _file_handlers() -> list:
    return [
        h for h in logging.getLogger(AUDIT_LOGGER_NAME).handlers
        if getattr(h, _FILE_HANDLER_TAG, None)
    ]


def test_no_file_handler_when_path_empty():
    configure_logging(level="INFO", audit=True, audit_file="")
    assert _file_handlers() == []


def test_creates_parent_directory(tmp_path):
    """RotatingFileHandler **不会**建父目录，logs/ 不存在时必须自己建。"""
    target = tmp_path / "nested" / "logs" / "audit.log"

    configure_logging(level="INFO", audit=True, audit_file=str(target))
    logging.getLogger(AUDIT_LOGGER_NAME).warning("测试一条")

    assert target.is_file()


def test_audit_line_lands_in_file(tmp_path):
    target = tmp_path / "audit.log"

    configure_logging(level="INFO", audit=True, audit_file=str(target))
    logging.getLogger(AUDIT_LOGGER_NAME).warning("动作已执行 action=restart_service")

    assert "restart_service" in target.read_text(encoding="utf-8")


def test_attach_is_idempotent(tmp_path):
    """`configure_logging` 每次 create_app 都会调用（测试里几十次）。

    无条件 addHandler 会让每条审计写 N 遍，且 N 个 handler 争抢同一文件——
    Windows 上轮转 rename 会直接 PermissionError。
    """
    target = tmp_path / "audit.log"

    configure_logging(level="INFO", audit=True, audit_file=str(target))
    configure_logging(level="INFO", audit=True, audit_file=str(target))
    configure_logging(level="INFO", audit=True, audit_file=str(target))

    assert len(_file_handlers()) == 1


def test_one_line_written_once(tmp_path):
    """幂等的可观测形式：重复配置后，一条日志在文件里只出现一次。"""
    target = tmp_path / "audit.log"
    configure_logging(level="INFO", audit=True, audit_file=str(target))
    configure_logging(level="INFO", audit=True, audit_file=str(target))

    logging.getLogger(AUDIT_LOGGER_NAME).warning("唯一标记 XYZ")

    assert target.read_text(encoding="utf-8").count("唯一标记 XYZ") == 1


def test_different_path_gets_its_own_handler(tmp_path):
    """换路径是另一回事（不是重复挂），应各自生效。"""
    configure_logging(level="INFO", audit=True, audit_file=str(tmp_path / "a.log"))
    configure_logging(level="INFO", audit=True, audit_file=str(tmp_path / "b.log"))

    assert len(_file_handlers()) == 2


# -- 既有语义不变 -------------------------------------------------------------
def test_audit_false_raises_level_but_still_logs_failures(tmp_path):
    """audit=false 的语义是「提到 WARNING」，**不是关闭审计**——成功静默、失败仍记。"""
    target = tmp_path / "audit.log"
    configure_logging(level="INFO", audit=False, audit_file=str(target))
    audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)

    audit_logger.info("成功执行（应被级别挡住）")
    audit_logger.error("执行失败 action=restart_service")

    content = target.read_text(encoding="utf-8")
    assert "执行失败" in content
    assert "成功执行" not in content


def test_audit_true_follows_root_level(tmp_path):
    """audit=true 时审计 logger 置 NOTSET → 跟随**根**级别。

    注意：pytest 自己会先把根 logger 配成 WARNING（它的 logging 插件），
    所以这里必须显式把根调成 INFO，否则 `basicConfig` 因「根已有 handler」而**不生效**，
    测试会变成在测 pytest 的环境而不是被测逻辑。
    """
    root = logging.getLogger()
    saved_level = root.level
    root.setLevel(logging.INFO)
    try:
        target = tmp_path / "audit.log"
        configure_logging(level="INFO", audit=True, audit_file=str(target))

        logging.getLogger(AUDIT_LOGGER_NAME).info("成功执行 action=cleanup_disk")

        assert "cleanup_disk" in target.read_text(encoding="utf-8")
    finally:
        root.setLevel(saved_level)


def test_unwritable_path_degrades_without_raising(tmp_path, monkeypatch):
    """落盘失败不该拖垮服务：退化成只进 console，不抛异常。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    configure_logging(level="INFO", audit=True, audit_file=str(blocker / "sub" / "audit.log"))

    assert _file_handlers() == []
