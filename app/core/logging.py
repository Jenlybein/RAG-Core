"""结构化日志：一次配置，三路输出。

控制台给人看（彩色文本），``logs/app.log`` 给机器看（全量 JSON Lines），
``logs/error.log`` 给排障看（只收 ERROR）。框架自身（uvicorn）的访问日志与
错误堆栈也一并落到这两个文件里。

设计要点：
- 输出仍走标准库 ``logging``，因此 ``RotatingFileHandler`` 与 pytest 的日志
  捕获都照常可用；structlog 只负责把事件字典渲染成最终字符串。
- ``setup_logging()`` 模块级幂等（``_configured`` 守卫），在 ``app.main`` 导入
  时调用一次，不进 lifespan、不依赖 import 顺序：uvicorn 会在导入应用之前就
  配置自己的 logger，而 ``dictConfig`` 会显式覆盖那三个 logger。
- 滚动阈值与份数只作为函数可选参数暴露（默认 10MB × 5），不进环境变量，
  方便测试用小阈值触发滚动。
"""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import merge_contextvars
from structlog.dev import ConsoleRenderer
from structlog.processors import EventRenamer, JSONRenderer, TimeStamper, format_exc_info
from structlog.stdlib import (
    BoundLogger,
    ExtraAdder,
    LoggerFactory,
    ProcessorFormatter,
    add_log_level,
    add_logger_name,
)

from app.core.config import settings

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

# 模块级幂等守卫：进程内重复导入 / 重复调用都只配置一次。
_configured = False

# structlog 自己的日志走这条链；非 structlog（含 uvicorn）的日志由
# ProcessorFormatter 的 foreign_pre_chain 走同一条链前四步，再补 ExtraAdder。
_SHARED_PROCESSORS: list[Any] = [
    merge_contextvars,
    add_log_level,
    add_logger_name,
    TimeStamper(fmt="iso", key="time"),
]
_FOREIGN_PRE_CHAIN: list[Any] = [*_SHARED_PROCESSORS, ExtraAdder()]

# 两个 formatter 共用同一套前处理；只有最后的渲染器不同。
# format_exc_info 把异常对象转成字符串，保证 JSONRenderer 不会因
# exc_info 里的 traceback 对象而抛 TypeError，堆栈才能真正落进文件。
_COMMON_POST_PROCESSORS: list[Any] = [
    ProcessorFormatter.remove_processors_meta,
    format_exc_info,
    EventRenamer("msg"),
]
_FILE_PROCESSORS: list[Any] = [*_COMMON_POST_PROCESSORS, JSONRenderer()]
_CONSOLE_PROCESSORS: list[Any] = [
    *_COMMON_POST_PROCESSORS,
    # event_key/timestamp_key 与上面的重命名的 key 对齐，控制台才不会把
    # 消息和时间戳降格成一串普通 key=value。
    ConsoleRenderer(colors=True, event_key="msg", timestamp_key="time"),
]

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def setup_logging(
    log_level: str | None = None,
    log_dir: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> None:
    """初始化日志系统；重复调用无副作用。

    ``log_level`` / ``log_dir`` 缺省时取 ``settings`` 的 ``LOG_LEVEL`` /
    ``LOG_DIR``；显式传入用于测试或特殊部署。``max_bytes`` / ``backup_count``
    只用于滚动策略，不进环境变量。
    """

    global _configured
    if _configured:
        return

    level = (log_level if log_level is not None else settings.LOG_LEVEL).upper()
    directory = Path(log_dir if log_dir is not None else settings.LOG_DIR)
    directory.mkdir(parents=True, exist_ok=True)

    handler_names = ["console", "app_file", "error_file"]
    handlers: dict[str, Any] = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
            "level": level,
        },
        "app_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "json",
            "level": level,
            "filename": str(directory / "app.log"),
            "maxBytes": max_bytes,
            "backupCount": backup_count,
            "encoding": "utf-8",
        },
        "error_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "json",
            "level": "ERROR",
            "filename": str(directory / "error.log"),
            "maxBytes": max_bytes,
            "backupCount": backup_count,
            "encoding": "utf-8",
        },
    }

    config: dict[str, Any] = {
        "version": 1,
        # uvicorn 在导入应用之前就建好了自己的 logger；不能把它们禁用。
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": ProcessorFormatter,
                "foreign_pre_chain": _FOREIGN_PRE_CHAIN,
                "processors": _FILE_PROCESSORS,
            },
            "console": {
                "()": ProcessorFormatter,
                "foreign_pre_chain": _FOREIGN_PRE_CHAIN,
                "processors": _CONSOLE_PROCESSORS,
            },
        },
        "handlers": handlers,
        "root": {"handlers": handler_names, "level": level},
        # uvicorn 0.35 默认把这三个 logger 设成 propagate=False 且自带
        # StreamHandler，只配 root 收不到访问日志与 500 堆栈，必须显式挂上。
        "loggers": {
            name: {"handlers": handler_names, "level": level, "propagate": False}
            for name in _UVICORN_LOGGERS
        },
    }

    logging.config.dictConfig(config)

    structlog.configure(
        processors=[*_SHARED_PROCESSORS, ProcessorFormatter.wrap_for_formatter],
        logger_factory=LoggerFactory(),
        wrapper_class=BoundLogger,
        cache_logger_on_first_use=False,
    )

    _configured = True
