"""
结构化 JSON 日志工具

提供 get_logger(name) 便捷函数，返回配置好的 logging.Logger 实例。
日志输出为结构化 JSON 格式，自动注入 thread_id 和 session_dir 作为 extra 字段。
日志级别由 LOG_LEVEL 环境变量控制，默认 INFO。
"""

import datetime
import json
import logging
import os
from typing import Any, Optional


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


class _JsonFormatter(logging.Formatter):
    """
    将日志记录格式化为结构化 JSON

    {
      "time": "2026-06-23T10:30:00.123Z",
      "level": "INFO",
      "logger": "tavily_tool",
      "message": "工具执行完成",
      "extra": {
        "thread_id": "abc123",
        "session_dir": "output/session_abc123",
        ...
      }
    }
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "time": datetime.datetime.fromtimestamp(
                record.created, tz=datetime.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 收集额外的结构化字段
        extra: dict[str, Any] = {}
        for key, value in record.__dict__.items():
            if key not in {
                "args",
                "asctime",
                "created",
                "exc_info",
                "exc_text",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            }:
                extra[key] = value

        if extra:
            log_entry["extra"] = extra

        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False, default=str)


def _inject_context(logger: logging.Logger) -> None:
    """
    包装 logger 的 _log 方法，自动注入当前 thread_id 和 session_dir 到 extra 字段
    """

    original_log = logger._log

    def _log_with_context(
        level: int,
        msg: str,
        args: tuple[Any, ...],
        exc_info: Any = None,
        extra: Optional[dict[str, Any]] = None,
        stack_info: bool = False,
        stacklevel: int = 1,
    ) -> None:
        if extra is None:
            extra = {}

        # 从 ContextVar 中注入上下文信息
        try:
            from app.api.context import get_session_context, get_thread_context

            thread_id = get_thread_context()
            # session_dir = get_session_context()

            if thread_id and "thread_id" not in extra:
                extra["thread_id"] = thread_id
            # if session_dir and "session_dir" not in extra:
            #     extra["session_dir"] = session_dir
        except ImportError:
            pass

        original_log(level, msg, args, exc_info=exc_info, extra=extra, stack_info=stack_info, stacklevel=stacklevel)

    logger._log = _log_with_context  # type: ignore[method-assign]


def get_logger(name: str) -> logging.Logger:
    """
    获取结构化 JSON 日志记录器

    自动注入 thread_id 和 session_dir 到 extra 字段，
    日志级别由 LOG_LEVEL 环境变量控制。

    :param name: 日志记录器名称，通常使用 __name__
    :return: 配置好的 logging.Logger 实例
    """
    logger = logging.getLogger(name)

    # 避免重复添加 handler
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)

    logger.setLevel(LOG_LEVEL)
    logger.propagate = False

    _inject_context(logger)

    return logger
