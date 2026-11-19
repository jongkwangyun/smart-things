from __future__ import annotations

import logging
from pathlib import Path

from server.logging_utils import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    KSTFormatter,
    get_rotating_kst_logger,
)

DEFAULT_SCHEDULER_LOG_FILE = Path("server/logs/scheduler.log")

__all__ = [
    "DEFAULT_LOG_BACKUP_COUNT",
    "DEFAULT_LOG_MAX_BYTES",
    "DEFAULT_SCHEDULER_LOG_FILE",
    "KSTFormatter",
    "get_scheduler_logger",
]


def get_scheduler_logger(
    log_file: Path | str = DEFAULT_SCHEDULER_LOG_FILE,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> logging.Logger:
    """스케줄러 전용 KST 포맷 회전 파일 로거 생성 또는 반환."""
    return get_rotating_kst_logger(
        "server.scheduler",
        log_file=log_file,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
