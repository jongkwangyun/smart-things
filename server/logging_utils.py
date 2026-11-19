from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from server.time_utils import KST

DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


class KSTFormatter(logging.Formatter):
    """KST(한국 표준시) 기준 일시 포맷터."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=KST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


def get_rotating_kst_logger(
    logger_name: str,
    log_file: Path | str,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> logging.Logger:
    """KST 포맷 RotatingFileHandler 로거를 생성하거나 기존 핸들러를 반환한다."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)

    log_path = Path(log_file).resolve()
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == log_path:
            return logger

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(
        KSTFormatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger
