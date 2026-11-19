from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.models import (
    CLEANING_TYPE_VACUUM,
    DEFAULT_CLEANING_TYPE,
    STATUS_FAILED,
    STATUS_MISSED,
    STATUS_SCHEDULED,
    STATUS_SUCCESS,
    Reservation,
)
from server.off_periods.auto_stop import AutoStopWatcher
from server.off_periods.auto_stop import is_cleaner_cleaning as _is_cleaner_cleaning
from server.off_periods.dnd_sync import DndSyncManager
from server.scheduler.backoff import (
    AUTH_BACKOFF_BASE_SECONDS,
    AUTH_BACKOFF_MAX_SECONDS,
    DEFAULT_AUTH_ALERT_COOLDOWN_SECONDS,
    DEFAULT_AUTH_ALERT_THRESHOLD,
    DEFAULT_DEVICE_ID,
    AuthBackoffTracker,
    StepStatusCache,
    resolve_default_client,
    resolve_default_device_id,
    send_failure_notification,
)
from server.scheduler.logger import DEFAULT_SCHEDULER_LOG_FILE, get_scheduler_logger
from server.smartthings.auth import TokenRefreshError
from server.smartthings.client import SmartThingsAuthError, SmartThingsClient, parse_failure_reason
from server.smartthings.health import apply_dnd_sync_outcome
from server.storage import Storage
from server.time_utils import format_kst, now_kst
from server.water.service import WaterService
from server.water.session_tracker import WaterSessionTracker

if TYPE_CHECKING:
    from server.notify.service import NotificationService

__all__ = ["DEFAULT_DEVICE_ID", "MISSED_THRESHOLD", "SUCCESS_WINDOW", "ReservationScheduler"]
MISSED_THRESHOLD = timedelta(minutes=5)
SUCCESS_WINDOW = timedelta(minutes=5)


class ReservationScheduler:
    """1회성 청소 예약 스케줄러 (D-08: 미니PC 자체 스케줄러, Rules API 미사용)."""

    def __init__(
        self, storage: Storage, client: SmartThingsClient | None = None, device_id: str | None = None,
        poll_interval: float = 1.0, now_func: Callable[[], datetime] = now_kst,
        log_file: Path | str = DEFAULT_SCHEDULER_LOG_FILE, notification_service: NotificationService | None = None,
        auto_stop_watcher: AutoStopWatcher | None = None, water_service: WaterService | None = None,
        dnd_sync_manager: DndSyncManager | None = None, water_session_tracker: WaterSessionTracker | None = None,
        auth_backoff_base: float = AUTH_BACKOFF_BASE_SECONDS, auth_backoff_max: float = AUTH_BACKOFF_MAX_SECONDS,
        auth_alert_threshold: int = DEFAULT_AUTH_ALERT_THRESHOLD,
        auth_alert_cooldown: float = DEFAULT_AUTH_ALERT_COOLDOWN_SECONDS,
    ) -> None:
        self.storage = storage
        self.log_file = Path(log_file)
        self.logger: logging.Logger = get_scheduler_logger(self.log_file)
        self.client: SmartThingsClient = client or resolve_default_client(self.log_file)
        self.device_id: str = device_id or resolve_default_device_id()
        self.poll_interval = max(0.05, float(poll_interval))
        self.now_func = now_func
        self.notification_service = notification_service
        self.dnd_sync_manager = dnd_sync_manager or DndSyncManager(self.storage, self.client, self.device_id)
        self.auto_stop_watcher = auto_stop_watcher or AutoStopWatcher(
            self.storage, self.client, self.device_id, self.notification_service, self.now_func
        )
        self.water_service = water_service or WaterService(self.storage, self.notification_service)
        self.water_session_tracker = water_session_tracker or WaterSessionTracker(
            self.storage, self.water_service, self.client, self.device_id, self.auto_stop_watcher, self.now_func
        )
        self.auth_backoff = AuthBackoffTracker(
            self.logger,
            base_seconds=auth_backoff_base,
            max_seconds=auth_backoff_max,
            alert_threshold=auth_alert_threshold,
            alert_cooldown_seconds=auth_alert_cooldown,
            notification_service_getter=lambda: self.notification_service,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def get_smartthings_health(self) -> dict[str, Any]:
        """현재 스마트싱스 인증 및 연동 상태 스냅샷을 반환한다."""
        return self.auth_backoff.to_health_dict(simulated=bool(getattr(self.client, "simulated", False)))

    @property
    def is_running(self) -> bool:
        """스케줄러 백그라운드 스레드 가동 여부."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def stop_requested(self) -> bool:
        """정상 종료(stop) 요청 여부."""
        return self._stop_event.is_set()

    def start(self) -> None:
        """백그라운드 폴링 데몬 스레드를 시작한다."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="ReservationSchedulerThread", daemon=True)
        self._thread.start()
        self.logger.info("스케줄러 시작 완료 (poll_interval=%.2fs, device_id=%s)", self.poll_interval, self.device_id)

    def stop(self, timeout: float = 5.0) -> None:
        """백그라운드 폴링 데몬 스레드를 안전하게 중지한다."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.logger.info("스케줄러 중지 완료")

    def step(self, now: datetime | None = None) -> list[Reservation]:
        """예약 상태를 검사하고, 1회 step 당 기기 상태를 최대 1번만 조회해 공유한다."""
        now = now if now is not None else self.now_func()
        reservations = self.storage.list_reservations()
        executed: list[Reservation] = []
        step_ctx = StepStatusCache(now, self.client, self.device_id, self.auth_backoff)

        for r in reservations:
            if r.status != STATUS_SCHEDULED or r.executed_at is not None:
                continue
            if (now - r.reserved_at) > MISSED_THRESHOLD:
                r.status, r.failure_reason, r.updated_at = STATUS_MISSED, "예약 시각 5분 경과로 누락 처리됨", now
                self.storage.save_reservation(r)
                self.logger.warning(
                    "예약 누락 처리 (5분 경과): id=%s, reserved_at=%s, now=%s",
                    r.id, format_kst(r.reserved_at), format_kst(now),
                )
                continue
            if r.reserved_at <= now and not step_ctx.halted:
                self._execute_reservation(r, now, step_ctx)
                executed.append(r)

        for r in reservations:
            if r.status == STATUS_SCHEDULED and r.executed_at is not None:
                if now <= r.reserved_at + SUCCESS_WINDOW:
                    if not step_ctx.halted and (st := step_ctx.get_status()) is not None:
                        self._mark_success_if_cleaning(r, st, now)
                else:
                    self._mark_failure(r, now, step_ctx)

        self._run_watchers(now, step_ctx)
        return executed

    def _clear_stale_auth_error(self, component: Any) -> None:
        if isinstance(getattr(component, "last_auth_error", None), Exception):
            component.last_auth_error = None

    def _consume_auth_error(self, component: Any, step_ctx: StepStatusCache, outcome: Any = None) -> None:
        if apply_dnd_sync_outcome(self.auth_backoff, component, step_ctx.now, outcome=outcome):
            step_ctx.halted = True

    def _run_watchers(self, now: datetime, step_ctx: StepStatusCache) -> None:
        """DND 동기화, 자동 정지, 물청소 세션 감시를 공유 기기 상태로 순차 실행한다."""
        if self.dnd_sync_manager is not None and not step_ctx.halted:
            self._clear_stale_auth_error(self.dnd_sync_manager)
            is_dnd = isinstance(self.dnd_sync_manager, DndSyncManager)
            needs_st = is_dnd and self.dnd_sync_manager.needs_device_status(now)
            dnd_st = step_ctx.get_status() if needs_st else None
            if not step_ctx.halted and (not needs_st or dnd_st is not None):
                dnd_out: Any = None
                try:
                    if is_dnd:
                        dnd_out = self.dnd_sync_manager.sync_with_outcome(now, device_status=dnd_st)
                    else:
                        self.dnd_sync_manager.sync(now)
                except Exception as e:
                    self.logger.exception("DND 동기화 매니저 실행 중 오류 발생: %s", e)
                self._consume_auth_error(self.dnd_sync_manager, step_ctx, outcome=dnd_out)

        if self.auto_stop_watcher is not None and not step_ctx.halted:
            self._clear_stale_auth_error(self.auto_stop_watcher)
            if self.auto_stop_watcher.notification_service is None and self.notification_service is not None:
                self.auto_stop_watcher.notification_service = self.notification_service
            try:
                if isinstance(self.auto_stop_watcher, AutoStopWatcher):
                    if self.auto_stop_watcher.needs_device_status(now) and (st := step_ctx.get_status()) is not None:
                        if not step_ctx.halted:
                            self.auto_stop_watcher.step(now, device_status=st)
                            self._consume_auth_error(self.auto_stop_watcher, step_ctx)
                else:
                    self.auto_stop_watcher.step(now)
                    self._consume_auth_error(self.auto_stop_watcher, step_ctx)
            except Exception as e:
                self.logger.exception("자동 정지 워처 실행 중 오류 발생: %s", e)

        if self.water_service is not None and self.water_service.notification_service is None:
            self.water_service.notification_service = self.notification_service

        if self.water_session_tracker is not None and not step_ctx.halted:
            try:
                if isinstance(self.water_session_tracker, WaterSessionTracker):
                    if (st := step_ctx.get_status()) is not None and not step_ctx.halted:
                        self.water_session_tracker.step(now, device_status=st)
                else:
                    self.water_session_tracker.step(now)
            except Exception as e:
                self.logger.exception("물청소 세션 트래커 실행 중 오류: %s", e)

    def _execute_reservation(self, r: Reservation, now: datetime, step_ctx: StepStatusCache) -> None:
        """예약 청소 시작 명령을 전송한다. 인증 오류 발생 시 현재 step 백오프를 기록한다."""
        r.executed_at, r.updated_at = now, now
        self.storage.save_reservation(r)
        self.logger.info(
            "청소 시작 명령 전송 시도: res_id=%s, reserved_at=%s, device_id=%s, cmd=start",
            r.id, format_kst(r.reserved_at), self.device_id,
        )
        try:
            ct = getattr(r, "cleaning_type", DEFAULT_CLEANING_TYPE)
            if hasattr(self.client, "set_cleaning_type") and callable(self.client.set_cleaning_type):
                self.client.set_cleaning_type(self.device_id, ct)
            res = self.client.start_cleaning(self.device_id)
            self.auth_backoff.record_success(now)
            self.logger.info(
                "청소 시작 명령 전송 성공: res_id=%s, reserved_at=%s, device_id=%s, result=%s",
                r.id, format_kst(r.reserved_at), self.device_id, res,
            )
        except (SmartThingsAuthError, TokenRefreshError) as e:
            self.logger.error("청소 시작 명령 전송 인증 실패: res_id=%s, error=%s", r.id, e)
            r.failure_reason, r.updated_at = f"명령 전송 실패: {e}", now
            self.storage.save_reservation(r)
            step_ctx.halt(e)
        except Exception as e:
            self.logger.error("청소 시작 명령 전송 실패: res_id=%s, error=%s", r.id, e)
            self.auth_backoff.record_api_error(now, e)
            r.failure_reason, r.updated_at = f"명령 전송 실패: {e}", now
            self.storage.save_reservation(r)

    def _mark_failure(self, r: Reservation, now: datetime, step_ctx: StepStatusCache) -> None:
        """예약 시각 +5분 초과 시 실패 처리 및 푸시 알림 전송 (REQ-008, REQ-009, D-10, D-12, LB-026)."""
        device_status: Any = None
        health = "UNKNOWN" if step_ctx.halted else "ONLINE"

        if not step_ctx.halted and callable(getattr(self.client, "get_device_health", None)):
            try:
                health = str(self.client.get_device_health(self.device_id) or "ONLINE").upper()
            except (SmartThingsAuthError, TokenRefreshError) as e:
                step_ctx.halt(e)
                health = "UNKNOWN"
            except Exception:
                health = "UNKNOWN"

        if not step_ctx.halted:
            device_status = step_ctx.get_status()
            if device_status is not None and getattr(device_status, "is_online", True) is False:
                health = "OFFLINE"
            elif device_status is None and health == "ONLINE":
                health = "OFFLINE"

        reason = parse_failure_reason(device_status, health_state=health, fallback_unknown=True)
        r.status, r.failure_reason, r.updated_at = STATUS_FAILED, (reason or r.failure_reason or "원인 미확인"), now
        r.last_device_state = getattr(device_status, "operating_state", None) if device_status else None
        self.storage.save_reservation(r)
        self.logger.warning("청소 시작 확인 실패: res_id=%s, failure_reason=%s", r.id, r.failure_reason)
        send_failure_notification(self.logger, self.notification_service, r.id, r.failure_reason)

    def _mark_success_if_cleaning(self, r: Reservation, status: Any, now: datetime) -> bool:
        """이미 조회된 기기 상태를 확인하여 청소 중이면 '성공'으로 상태를 갱신한다 (REQ-007, D-10)."""
        if not _is_cleaner_cleaning(status):
            return False
        op_state = getattr(status, "operating_state", None) or "cleaning"
        r.status, r.last_device_state, r.updated_at = STATUS_SUCCESS, str(op_state), now
        self.storage.save_reservation(r)
        try:
            if self.water_session_tracker is not None:
                self.water_session_tracker.notify_reservation_success(r.id, getattr(r, "cleaning_type", None), now)
            elif self.water_service is not None:
                is_mop = getattr(r, "cleaning_type", DEFAULT_CLEANING_TYPE) != CLEANING_TYPE_VACUUM
                self.water_service.record_mop_clean(is_mop=is_mop)
        except Exception as e:
            self.logger.warning("물청소 기록 처리 실패: %s", e)
        self.logger.info(
            "청소 시작 확인 완료 (성공): res_id=%s, reserved_at=%s, state=%s",
            r.id, format_kst(r.reserved_at), r.last_device_state,
        )
        return True

    def _run_loop(self) -> None:
        """백그라운드 폴링 루프."""
        try:
            while not self._stop_event.is_set():
                try:
                    self.step()
                except Exception as e:
                    self.logger.exception("스케줄러 주기 실행 중 오류 발생: %s", e)
                self._stop_event.wait(self.poll_interval)
        finally:
            if not self._stop_event.is_set():
                self.logger.error("스케줄러 폴링 스레드가 예기치 않게 중단되었습니다.")
