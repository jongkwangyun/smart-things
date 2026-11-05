from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.models import STATUS_FAILED, STATUS_MISSED, STATUS_SCHEDULED, STATUS_SUCCESS, Reservation
from server.off_periods.auto_stop import is_cleaner_cleaning as _is_cleaner_cleaning
from server.scheduler.logger import DEFAULT_SCHEDULER_LOG_FILE, get_scheduler_logger
from server.smartthings.auth import SmartThingsTokenManager, TokenInfo, resolve_credential
from server.smartthings.client import SmartThingsClient, parse_failure_reason
from server.storage import Storage
from server.time_utils import format_kst, now_kst

if TYPE_CHECKING:
    from server.notify.service import NotificationService
    from server.off_periods.auto_stop import AutoStopWatcher
    from server.off_periods.dnd_sync import DndSyncManager
    from server.water.service import WaterService

DEFAULT_DEVICE_ID = "03431570-5acf-f89e-7b39-275ce15d3258"
MISSED_THRESHOLD = timedelta(minutes=5)
SUCCESS_WINDOW = timedelta(minutes=5)


def _resolve_default_client(log_file: Path | str = DEFAULT_SCHEDULER_LOG_FILE) -> SmartThingsClient:
    """기본 스마트싱스 클라이언트를 생성한다. 토큰 부재 시 시뮬레이션 클라이언트로 대체한다."""
    try:
        return SmartThingsClient(token_manager=SmartThingsTokenManager.from_env())
    except Exception:
        dummy_info = TokenInfo(access_token="simulated-scheduler-token", token_type="pat")
        return SmartThingsClient(
            token_manager=SmartThingsTokenManager(token_info=dummy_info, log_file=log_file),
            simulated=True,
        )


def _resolve_default_device_id() -> str:
    """설정된 기기 ID를 반환하거나 기본 VR7MD97716G ID를 반환한다."""
    return resolve_credential("SMARTTHINGS_DEVICE_ID") or DEFAULT_DEVICE_ID


class ReservationScheduler:
    """1회성 청소 예약 스케줄러 (D-08: 미니PC 자체 스케줄러, Rules API 미사용)."""

    def __init__(
        self,
        storage: Storage,
        client: SmartThingsClient | None = None,
        device_id: str | None = None,
        poll_interval: float = 1.0,
        now_func: Callable[[], datetime] = now_kst,
        log_file: Path | str = DEFAULT_SCHEDULER_LOG_FILE,
        notification_service: NotificationService | None = None,
        auto_stop_watcher: AutoStopWatcher | None = None,
        water_service: WaterService | None = None,
        dnd_sync_manager: DndSyncManager | None = None,
    ) -> None:
        self.storage = storage
        self.log_file = Path(log_file)
        self.logger: logging.Logger = get_scheduler_logger(self.log_file)
        self.client: SmartThingsClient = client or _resolve_default_client(self.log_file)
        self.device_id: str = device_id or _resolve_default_device_id()
        self.poll_interval = max(0.05, float(poll_interval))
        self.now_func = now_func
        self.notification_service = notification_service
        if dnd_sync_manager is not None:
            self.dnd_sync_manager = dnd_sync_manager
        else:
            from server.off_periods.dnd_sync import DndSyncManager
            self.dnd_sync_manager = DndSyncManager(
                storage=self.storage, client=self.client, device_id=self.device_id,
            )
        if auto_stop_watcher is not None:
            self.auto_stop_watcher = auto_stop_watcher
        else:
            from server.off_periods.auto_stop import AutoStopWatcher
            self.auto_stop_watcher = AutoStopWatcher(
                storage=self.storage,
                client=self.client,
                device_id=self.device_id,
                notification_service=self.notification_service,
                now_func=self.now_func,
            )
        if water_service is not None:
            self.water_service = water_service
        else:
            from server.water.service import WaterService
            self.water_service = WaterService(
                storage=self.storage,
                notification_service=self.notification_service,
            )

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """스케줄러 백그라운드 스레드 가동 여부."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """백그라운드 폴링 데몬 스레드를 시작한다."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ReservationSchedulerThread",
            daemon=True,
        )
        self._thread.start()
        self.logger.info("스케줄러 시작 완료 (poll_interval=%.2fs, device_id=%s)", self.poll_interval, self.device_id)

    def stop(self, timeout: float = 5.0) -> None:
        """백그라운드 폴링 데몬 스레드를 안전하게 중지한다."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.logger.info("스케줄러 중지 완료")

    def step(self, now: datetime | None = None) -> list[Reservation]:
        """예약 상태를 검사하고 예약 시각이 된 청소 시작 명령을 실행한다 (REQ-005, D-08).

        - 예정(STATUS_SCHEDULED) 상태이면서 아직 실행되지 않은 예약(executed_at is None) 대상
        - 예약 시각 +5분을 초과한 예약은 청소를 실행하지 않고 '누락' 처리 (REQ-027, LB-028)
        - 명령 전송 직전에 영구 저장소에 executed_at을 기록하여 중복 실행 방지 (REQ-006, LB-016)
        - 청소 시작 명령(start) 전송 후 server/logs/scheduler.log에 기록
        - 예약 시각 ±5분 내 청소 시작 확인 후 '성공' 상태 전이 (REQ-007, D-10, LB-017)
        - 쉬는 기간 DND 동기화(REQ-016, LB-045) 및 AutoStop 2차 방어선 유지
        """
        now = now if now is not None else self.now_func()
        reservations = self.storage.list_reservations()
        executed: list[Reservation] = []

        for r in reservations:
            # 1. 미실행 예약 처리 (REQ-005, REQ-027)
            if r.status == STATUS_SCHEDULED and r.executed_at is None:
                if (now - r.reserved_at) > MISSED_THRESHOLD:
                    r.status, r.failure_reason, r.updated_at = STATUS_MISSED, "예약 시각 5분 경과로 누락 처리됨", now
                    self.storage.save_reservation(r)
                    self.logger.warning(
                        "예약 누락 처리 (5분 경과): id=%s, reserved_at=%s, now=%s",
                        r.id, format_kst(r.reserved_at), format_kst(now),
                    )
                    continue

                if r.reserved_at <= now:
                    r.executed_at, r.updated_at = now, now
                    self.storage.save_reservation(r)
                    self.logger.info(
                        "청소 시작 명령 전송 시도: res_id=%s, reserved_at=%s, device_id=%s, cmd=start",
                        r.id, format_kst(r.reserved_at), self.device_id,
                    )
                    try:
                        res = self.client.start_cleaning(self.device_id)
                        self.logger.info(
                            "청소 시작 명령 전송 성공: res_id=%s, reserved_at=%s, device_id=%s, result=%s",
                            r.id, format_kst(r.reserved_at), self.device_id, res,
                        )
                    except Exception as e:
                        self.logger.error(
                            "청소 시작 명령 전송 실패: res_id=%s, reserved_at=%s, device_id=%s, error=%s",
                            r.id, format_kst(r.reserved_at), self.device_id, e,
                        )
                        r.failure_reason, r.updated_at = f"명령 전송 실패: {e}", now
                        self.storage.save_reservation(r)

                    executed.append(r)

            # 2. 실행 성공/실패 판정 (REQ-007, REQ-008, REQ-009, D-10, LB-017, LB-026)
            if r.status == STATUS_SCHEDULED and r.executed_at is not None:
                if now <= r.reserved_at + SUCCESS_WINDOW:
                    self._check_and_mark_success(r, now)
                else:
                    self._mark_failure(r, now)

        if self.dnd_sync_manager is not None:
            try:
                self.dnd_sync_manager.sync(now)
            except Exception as e:
                self.logger.exception("DND 동기화 매니저 실행 중 오류 발생: %s", e)

        if self.auto_stop_watcher is not None:
            if self.auto_stop_watcher.notification_service is None and self.notification_service is not None:
                self.auto_stop_watcher.notification_service = self.notification_service
            try:
                self.auto_stop_watcher.step(now)
            except Exception as e:
                self.logger.exception("자동 정지 워처 실행 중 오류 발생: %s", e)

        if (
            self.water_service is not None
            and self.water_service.notification_service is None
            and self.notification_service is not None
        ):
            self.water_service.notification_service = self.notification_service

        return executed

    def _mark_failure(self, r: Reservation, now: datetime) -> None:
        """예약 시각 +5분 초과 시 실패 처리 및 푸시 알림 전송 (REQ-008, REQ-009, D-10, D-12, LB-026)."""
        device_status: Any = None
        health = "ONLINE"
        if hasattr(self.client, "get_device_health") and callable(self.client.get_device_health):
            try:
                health = str(self.client.get_device_health(self.device_id) or "ONLINE").upper()
            except Exception:
                health = "UNKNOWN"

        if hasattr(self.client, "get_device_status") and callable(self.client.get_device_status):
            try:
                device_status = self.client.get_device_status(self.device_id)
                if getattr(device_status, "is_online", True) is False:
                    health = "OFFLINE"
            except Exception as e:
                self.logger.warning("실패 원인 확인 중 기기 조회 실패: res_id=%s, error=%s", r.id, e)
                if health == "ONLINE":
                    health = "OFFLINE"

        reason = parse_failure_reason(device_status, health_state=health, fallback_unknown=True)
        r.status = STATUS_FAILED
        r.failure_reason = reason or r.failure_reason or "원인 미확인"
        r.last_device_state = getattr(device_status, "operating_state", None) if device_status else None
        r.updated_at = now
        self.storage.save_reservation(r)
        self.logger.warning(
            "청소 시작 확인 실패: res_id=%s, reserved_at=%s, failure_reason=%s, last_device_state=%s",
            r.id, format_kst(r.reserved_at), r.failure_reason, r.last_device_state,
        )

        if self.notification_service is not None:
            title = "[로봇청소기] 청소 실패"
            body = f"예약 청소가 시작되지 않았습니다: {r.failure_reason}"
            data = {"type": "cleaning_failed", "reservation_id": r.id, "failure_reason": r.failure_reason or ""}
            try:
                send_fn = getattr(self.notification_service, "send_to_all", None) or getattr(
                    self.notification_service, "send_multicast", None
                )
                if callable(send_fn):
                    res = send_fn(title=title, body=body, data=data)
                    self.logger.info(
                        "청소 실패 알림 발송 완료: res_id=%s, targets=%d, success=%d, failure=%d",
                        r.id, getattr(res, "total_targets", 0), getattr(res, "success_count", 0),
                        getattr(res, "failure_count", 0),
                    )
            except Exception as e:
                self.logger.error("실패 푸시 알림 발송 중 오류: res_id=%s, error=%s", r.id, e)

    def _check_and_mark_success(self, r: Reservation, now: datetime) -> bool:
        """기기 상태를 조회하여 청소 중 상태이면 '성공'으로 상태를 갱신한다 (REQ-007, D-10)."""
        if not hasattr(self.client, "get_device_status") or not callable(
            getattr(self.client, "get_device_status", None)
        ):
            return False

        try:
            status = self.client.get_device_status(self.device_id)
        except Exception as e:
            self.logger.warning(
                "청소 상태 확인 중 기기 조회 실패: res_id=%s, device_id=%s, error=%s",
                r.id, self.device_id, e,
            )
            return False

        if _is_cleaner_cleaning(status):
            op_state = getattr(status, "operating_state", None) or "cleaning"
            r.status = STATUS_SUCCESS
            r.last_device_state = str(op_state)
            r.updated_at = now
            self.storage.save_reservation(r)
            if self.water_service is not None:
                try:
                    self.water_service.record_mop_clean(is_mop=True)
                except Exception as e:
                    self.logger.warning("물걸레 청소 횟수 기록 실패: %s", e)
            self.logger.info(
                "청소 시작 확인 완료 (성공): res_id=%s, reserved_at=%s, device_id=%s, state=%s, confirmed_at=%s",
                r.id, format_kst(r.reserved_at), self.device_id, r.last_device_state, format_kst(now),
            )
            return True
        return False

    def _run_loop(self) -> None:
        """백그라운드 폴링 루프."""
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception as e:
                self.logger.exception("스케줄러 주기 실행 중 오류 발생: %s", e)
            self._stop_event.wait(self.poll_interval)
