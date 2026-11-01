from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from server.models import AutoStopEvent, OffPeriod
from server.time_utils import now_kst, to_kst

if TYPE_CHECKING:
    from server.notify.service import NotificationService
    from server.smartthings.client import SmartThingsClient
    from server.storage import Storage

DEFAULT_DEVICE_ID = "03431570-5acf-f89e-7b39-275ce15d3258"


def is_cleaner_cleaning(status: Any) -> bool:
    """청소기가 청소 중 상태인지 확인한다 (docs/device-capabilities.md 섹션 4)."""
    if status is None:
        return False
    op_state = getattr(status, "operating_state", None) or (
        status.get("operating_state") if isinstance(status, dict) else None
    )
    if str(op_state or "").lower() in ("cleaning", "processing"):
        return True
    movement = getattr(status, "movement", None) or (
        status.get("movement") if isinstance(status, dict) else None
    )
    if str(movement or "").lower() == "cleaning":
        return True
    raw = getattr(status, "raw_status", None) or (
        status.get("raw_status") if isinstance(status, dict) else None
    )
    if isinstance(raw, dict):
        main = raw.get("components", {}).get("main", {}) if isinstance(raw.get("components"), dict) else {}
        for key in ("robotCleanerMovement", "samsungce.robotCleanerMovement"):
            comp = main.get(key) if isinstance(main, dict) else None
            if isinstance(comp, dict):
                for subkey in ("movement", "robotCleanerMovement", "value"):
                    sub = comp.get(subkey)
                    val = sub.get("value") if isinstance(sub, dict) else sub
                    if str(val or "").lower() == "cleaning":
                        return True
    return False


class AutoStopWatcher:
    """쉬는 기간 중 반복 예약 청소 감지 및 자동 정지 워처 (REQ-016, D-04, D-05, D-11)."""

    def __init__(
        self,
        storage: Storage,
        client: SmartThingsClient,
        device_id: str = DEFAULT_DEVICE_ID,
        notification_service: NotificationService | None = None,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.client = client
        self.device_id = device_id
        self.notification_service = notification_service
        self.now_func = now_func or now_kst
        self.logger: logging.Logger = logging.getLogger("server.off_periods.auto_stop")

    def get_active_off_period(self, now: datetime) -> OffPeriod | None:
        """지정 시각이 포함된 활성 쉬는 기간을 반환한다."""
        now_k = to_kst(now)
        for op in self.storage.list_off_periods():
            if to_kst(op.start_at) <= now_k <= to_kst(op.end_at):
                return op
        return None

    def is_allowed_reservation_cleaning(self, now: datetime) -> bool:
        """쉬는 기간 중 허용된 1회성 예약 청소인지 확인한다 (REQ-015, D-05, LB-031)."""
        now_k = to_kst(now)
        for r in self.storage.list_reservations():
            if not r.allow_during_off_period:
                continue
            if r.executed_at is None:
                continue
            delta = now_k - to_kst(r.executed_at)
            if timedelta(seconds=0) <= delta <= timedelta(minutes=10):
                return True
        return False

    def step(self, now: datetime | None = None) -> AutoStopEvent | None:
        """기기 상태를 점검하여 쉬는 기간 중 반복 예약 청소를 즉시 정지한다 (REQ-016, D-04, D-11)."""
        current_time = to_kst(now) if now is not None else self.now_func()
        op = self.get_active_off_period(current_time)
        if op is None:
            return None

        if self.is_allowed_reservation_cleaning(current_time):
            return None

        try:
            device_status = self.client.get_device_status(self.device_id)
        except Exception as e:
            self.logger.warning("자동 정지 기기 상태 조회 실패: device_id=%s, error=%s", self.device_id, e)
            return None

        if not is_cleaner_cleaning(device_status):
            return None

        t0 = time.perf_counter()
        success = True
        try:
            self.client.return_to_home(self.device_id)
        except Exception as e:
            success = False
            self.logger.error("자동 정지 명령(returnToHome) 전송 실패: %s", e)
        elapsed = time.perf_counter() - t0

        op_state = getattr(device_status, "operating_state", None) or "cleaning"
        if success:
            details = (
                f"쉬는 기간({op.id}) 중 청소 시작 감지(상태: {op_state})로 정지 명령 전송. "
                f"정지 소요 시간: {elapsed:.3f}초"
            )
        else:
            details = (
                f"쉬는 기간({op.id}) 중 청소 시작 감지(상태: {op_state})로 정지 명령 전송 실패. "
                f"정지 소요 시간: {elapsed:.3f}초"
            )

        event = AutoStopEvent(
            id=f"autostop-{uuid.uuid4().hex[:8]}",
            occurred_at=current_time,
            off_period_id=op.id,
            success=success,
            details=details,
        )
        self.storage.save_auto_stop_event(event)
        self.logger.warning(
            "쉬는 기간 반복 예약 청소 감지 및 자동 정지 실행: event_id=%s, off_period_id=%s, elapsed=%.3fs",
            event.id,
            op.id,
            elapsed,
        )

        if self.notification_service is not None and success:
            title = "[로봇청소기] 쉬는 기간 자동 정지"
            body = f"쉬는 기간 중 청소가 시작되어 즉시 정지시켰습니다. (정지 소요: {elapsed:.2f}초)"
            data = {
                "type": "auto_stop",
                "off_period_id": op.id,
                "event_id": event.id,
                "elapsed": f"{elapsed:.3f}",
            }
            try:
                send_fn = getattr(self.notification_service, "send_to_all", None) or getattr(
                    self.notification_service, "send_multicast", None
                )
                if callable(send_fn):
                    send_fn(title=title, body=body, data=data)
            except Exception as e:
                self.logger.error("자동 정지 푸시 알림 발송 중 오류: %s", e)

        return event


AutoStopService = AutoStopWatcher

__all__ = [
    "DEFAULT_DEVICE_ID",
    "AutoStopService",
    "AutoStopWatcher",
    "is_cleaner_cleaning",
]
