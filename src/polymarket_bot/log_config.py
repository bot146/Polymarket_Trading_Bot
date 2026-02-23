from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler

_LOG_DIR = Path("logs")
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
_BACKUP_COUNT = 3  # keep 3 rotated files


def setup_logging(level: str = "INFO") -> None:
    _LOG_DIR.mkdir(exist_ok=True)

    file_handler = RotatingFileHandler(
        _LOG_DIR / "bot.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True), file_handler],
        force=True,
    )
