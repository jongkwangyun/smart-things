from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.models import (
    STATUS_MISSED,
    STATUS_SCHEDULED,
    STATUS_SUCCESS,
    Reservation,
)
from server.scheduler.logger import (
    DEFAULT_SCHEDULER_LOG_FILE,
    get_scheduler_logger,
)
from server.smartthings.auth import (
    SmartThingsTokenManager,
    TokenInfo,
    resolve_credential,
)
from server.smartthings.client import SmartThingsClient
from server.storage import Storage
from server.time_utils import format_kst, now_kst

DEFAULT_DEVICE_ID = "03431570-5acf-f89e-7b39-275ce15d3258"
MISSED_THRESHOLD = timedelta(minutes=5)
SUCCESS_WINDOW = timedelta(minutes=5)


def _is_cleaner_cleaning(status: Any) -> bool:
    """청소기가 청소 중 상태인지 확인한다 (docs/device-capabilities.md 섹션 4)."""
    if status is None:
        return False

    op_state = getattr(status, "operating_state", None)
    if op_state is None and isinstance(status, dict):
        op_state = status.get("operating_state")
    if str(op_state or "").lower() in ("cleaning", "processing"):
        return True

    movement = getattr(status, "movement", None)
    if movement is None and isinstance(status, dict):
        movement = status.get("movement")
    if str(movement or "").lower() == "cleaning":
        return True

    raw = getattr(status, "raw_status", None)
    if raw is None and isinstance(status, dict):
        raw = status.get("raw_status")
    if isinstance(raw, dict):
        components = raw.get("components", {})
        main = components.get("main", {}) if isinstance(components, dict) else {}
        for key in ("robotCleanerMovement", "samsungce.robotCleanerMovement"):
            comp_movement = main.get(key)
            if isinstance(comp_movement, dict):
                for subkey in ("movement", "robotCleanerMovement", "value"):
                    sub_val = comp_movement.get(subkey)
                    if isinstance(sub_val, dict) and str(sub_val.get("value", "")).lower() == "cleaning":
                        return True
                    if isinstance(sub_val, str) and sub_val.lower() == "cleaning":
                        return True
    return False


def _resolve_default_client(log_file: Path | str = DEFAULT_SCHEDULER_LOG_FILE) -> SmartThingsClient:
    """기본 스마트싱스 클라이언트를 생성한다. 토큰 부재 시 시뮬레이션 클라이언트로 대체한다."""
    try:
        token_mgr = SmartThingsTokenManager.from_env()
        return SmartThingsClient(token_manager=token_mgr)
    except Exception:
        dummy_info = TokenInfo(access_token="simulated-scheduler-token", token_type="pat")
        token_mgr = SmartThingsTokenManager(token_info=dummy_info, log_file=log_file)
        return SmartThingsClient(token_manager=token_mgr, simulated=True)


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
    ) -> None:
        self.storage = storage
        self.log_file = Path(log_file)
        self.logger: logging.Logger = get_scheduler_logger(self.log_file)
        self.client: SmartThingsClient = client or _resolve_default_client(self.log_file)
        self.device_id: str = device_id or _resolve_default_device_id()
        self.poll_interval = max(0.05, float(poll_interval))
        self.now_func = now_func

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

    def step(self) -> list[Reservation]:
        """예약 상태를 검사하고 예약 시각이 된 청소 시작 명령을 실행한다 (REQ-005, D-08).

        - 예정(STATUS_SCHEDULED) 상태이면서 아직 실행되지 않은 예약(executed_at is None) 대상
        - 예약 시각 +5분을 초과한 예약은 청소를 실행하지 않고 '누락' 처리 (REQ-027, LB-028)
        - 명령 전송 직전에 영구 저장소에 executed_at을 기록하여 중복 실행 방지 (REQ-006, LB-016)
        - 청소 시작 명령(start) 전송 후 server/logs/scheduler.log에 기록
        - 예약 시각 ±5분 내 청소 시작 확인 후 '성공' 상태 전이 (REQ-007, D-10, LB-017)
        """
        now = self.now_func()
        reservations = self.storage.list_reservations()
        executed: list[Reservation] = []

        for r in reservations:
            # 1. 미실행 예약 처리 (REQ-005, REQ-027)
            if r.status == STATUS_SCHEDULED and r.executed_at is None:
                # 5분 초과 지연된 예약: 누락 처리 (REQ-027)
                if (now - r.reserved_at) > MISSED_THRESHOLD:
                    r.status = STATUS_MISSED
                    r.failure_reason = "예약 시각 5분 경과로 누락 처리됨"
                    r.updated_at = now
                    self.storage.save_reservation(r)
                    self.logger.warning(
                        "예약 누락 처리 (5분 경과): id=%s, reserved_at=%s, now=%s",
                        r.id,
                        format_kst(r.reserved_at),
                        format_kst(now),
                    )
                    continue

                # 예약 시각 도달 시 실행
                if r.reserved_at <= now:
                    # 1. 중복 실행 방지를 위해 명령 전송 전 영구 저장소에 실행 일시 기록 (LB-016)
                    r.executed_at = now
                    r.updated_at = now
                    self.storage.save_reservation(r)

                    # 2. 청소기 시작 명령 전송
                    self.logger.info(
                        "청소 시작 명령 전송 시도: res_id=%s, reserved_at=%s, device_id=%s, cmd=start",
                        r.id,
                        format_kst(r.reserved_at),
                        self.device_id,
                    )
                    try:
                        res = self.client.start_cleaning(self.device_id)
                        self.logger.info(
                            "청소 시작 명령 전송 성공: res_id=%s, reserved_at=%s, device_id=%s, result=%s",
                            r.id,
                            format_kst(r.reserved_at),
                            self.device_id,
                            res,
                        )
                    except Exception as e:
                        self.logger.error(
                            "청소 시작 명령 전송 실패: res_id=%s, reserved_at=%s, device_id=%s, error=%s",
                            r.id,
                            format_kst(r.reserved_at),
                            self.device_id,
                            e,
                        )
                        r.failure_reason = f"명령 전송 실패: {e}"
                        r.updated_at = now
                        self.storage.save_reservation(r)

                    executed.append(r)

            # 2. 실행 성공 판정 (REQ-007, D-10, LB-017)
            # 예약 시각 ±5분 이내에 청소기 상태가 '청소 중'으로 바뀌면 '성공'으로 표시
            if r.status == STATUS_SCHEDULED and r.executed_at is not None:
                window_start = r.reserved_at - SUCCESS_WINDOW
                window_end = r.reserved_at + SUCCESS_WINDOW
                if window_start <= now <= window_end:
                    self._check_and_mark_success(r, now)

        return executed

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
                r.id,
                self.device_id,
                e,
            )
            return False

        if _is_cleaner_cleaning(status):
            op_state = getattr(status, "operating_state", None) or "cleaning"
            r.status = STATUS_SUCCESS
            r.last_device_state = str(op_state)
            r.updated_at = now
            self.storage.save_reservation(r)
            self.logger.info(
                "청소 시작 확인 완료 (성공): res_id=%s, reserved_at=%s, device_id=%s, state=%s, confirmed_at=%s",
                r.id,
                format_kst(r.reserved_at),
                self.device_id,
                r.last_device_state,
                format_kst(now),
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
