from __future__ import annotations

import contextlib
from datetime import datetime
from http import HTTPStatus
from typing import Any

from server.smartthings.auth import TokenRefreshError
from server.smartthings.client import SmartThingsAuthError
from server.time_utils import format_kst, now_kst


def apply_dnd_sync_outcome(
    auth_backoff: Any,
    comp: Any,
    now: datetime,
    exc: Exception | None = None,
    outcome: Any = None,
) -> bool:
    """DndSyncManager/AutoStopWatcher 결과를 백오프 추적기에 반영한다. 인증 오류 시 True를 반환한다."""
    if exc is not None:
        if isinstance(exc, (SmartThingsAuthError, TokenRefreshError)):
            auth_backoff.record_failure(now, exc)
            return True
        auth_backoff.record_api_error(now, exc)
        return False
    if outcome is not None:
        auth_err = getattr(outcome, "auth_error", None)
        if isinstance(auth_err, Exception):
            auth_backoff.record_failure(now, auth_err)
            return True
        if getattr(outcome, "succeeded", False):
            auth_backoff.record_success(now)
        elif getattr(outcome, "failed", False):
            err_msg = getattr(outcome, "error_message", None) or "DND 동기화 실패"
            auth_backoff.record_api_error(now, RuntimeError(err_msg))
        return False

    lock = getattr(comp, "_lock", None)
    with (lock if lock is not None else contextlib.nullcontext()):
        auth_err = getattr(comp, "last_auth_error", None)
        succeeded = bool(getattr(comp, "last_sync_succeeded", False))
        failed = bool(getattr(comp, "last_sync_failed", False))
        err_msg = getattr(comp, "last_error", None)
        for attr, default in (
            ("last_auth_error", None),
            ("last_sync_succeeded", False),
            ("last_sync_failed", False),
            ("last_error", None),
        ):
            if hasattr(comp, attr):
                setattr(comp, attr, default)
    if isinstance(auth_err, Exception):
        auth_backoff.record_failure(now, auth_err)
        return True
    if succeeded:
        auth_backoff.record_success(now)
    elif failed:
        auth_backoff.record_api_error(now, RuntimeError(err_msg or "DND 동기화 실패"))
    return False


def get_smartthings_health(scheduler: Any, fallback_backoff: Any, dnd_sync_manager: Any = None) -> dict[str, Any]:
    """스케줄러 또는 폴백 백오프 상태로부터 스마트싱스 연동 상태 스냅샷을 반환한다."""
    if scheduler is not None:
        return dict(scheduler.get_smartthings_health())
    client = getattr(dnd_sync_manager, "client", None)
    return dict(fallback_backoff.to_health_dict(simulated=bool(getattr(client, "simulated", False))))


def is_scheduler_degraded(server: Any) -> bool:
    """스케줄러가 기대되는 서버에서 예기치 않게 중단되었는지 판정한다."""
    if not getattr(server, "scheduler_expected", False) or getattr(server, "is_closing", False):
        return False
    scheduler = getattr(server, "scheduler", None)
    if scheduler is None:
        return True
    return not bool(scheduler.is_running) and not bool(getattr(scheduler, "stop_requested", False))


def build_health_response(server: Any) -> tuple[int, dict[str, Any]]:
    """GET /health HTTP 상태 코드와 응답 페이로드를 생성한다."""
    scheduler = getattr(server, "scheduler", None)
    scheduler_running = bool(scheduler is not None and scheduler.is_running)
    degraded = is_scheduler_degraded(server)
    status_code = int(HTTPStatus.SERVICE_UNAVAILABLE if degraded else HTTPStatus.OK)
    payload: dict[str, Any] = {
        "status": "degraded" if degraded else "ok",
        "service": "smart-things-mini-pc",
        "time_kst": format_kst(now_kst()),
        "database": "connected",
        "scheduler_running": scheduler_running,
        **get_smartthings_health(scheduler, server._fallback_backoff, server.dnd_sync_manager),
    }
    return status_code, payload


def build_health_payload(server: Any) -> dict[str, Any]:
    """GET /health 응답 페이로드를 생성한다 (기존 필드 유지 + 스케줄러/스마트싱스 상태 추가)."""
    _, payload = build_health_response(server)
    return payload


def get_server_smartthings_health(server: Any) -> dict[str, Any]:
    """MiniPCServer 인스턴스 기준 스마트싱스 연동 상태 스냅샷을 반환한다."""
    return get_smartthings_health(server.scheduler, server._fallback_backoff, server.dnd_sync_manager)


def record_server_dnd_outcome(
    server: Any,
    dnd_m: Any,
    now: datetime,
    exc: Exception | None = None,
    outcome: Any = None,
) -> None:
    """MiniPCServer의 스케줄러 또는 폴백 백오프에 DND 동기화 결과를 반영한다."""
    tracker = server.scheduler.auth_backoff if server.scheduler is not None else server._fallback_backoff
    apply_dnd_sync_outcome(tracker, dnd_m, now, exc=exc, outcome=outcome)
