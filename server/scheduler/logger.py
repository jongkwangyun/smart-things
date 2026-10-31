from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from server.time_utils import KST

DEFAULT_SCHEDULER_LOG_FILE = Path("server/logs/scheduler.log")


class KSTFormatter(logging.Formatter):
    """KST(한국 표준시) 기준 일시 포맷터."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=KST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


def get_scheduler_logger(log_file: Path | str = DEFAULT_SCHEDULER_LOG_FILE) -> logging.Logger:
    """스케줄러 전용 KST 포맷 파일 로거 생성 또는 반환."""
    logger = logging.getLogger("server.scheduler")
    logger.setLevel(logging.INFO)

    log_path = Path(log_file).resolve()
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == log_path:
            return logger

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    formatter = KSTFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
