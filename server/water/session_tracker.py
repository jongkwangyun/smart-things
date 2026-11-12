from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.models import CLEANING_TYPE_VACUUM
from server.off_periods.auto_stop import is_cleaner_cleaning
from server.time_utils import format_kst, now_kst

if TYPE_CHECKING:
    from server.models import WaterTracker
    from server.smartthings.client import SmartThingsClient
    from server.storage import Storage
    from server.water.service import WaterService

DEFAULT_DEVICE_ID = "03431570-5acf-f89e-7b39-275ce15d3258"
WATER_CLEANING_TYPES: tuple[str, ...] = ("mop", "vacuumandmoptogether", "mopaftervacuum")
SESSION_OPERATING_STATES: tuple[str, ...] = ("cleaning", "washingmop", "processing")


def extract_cleaning_type(status: Any) -> str:
    """기기 상태 객체 또는 raw dict에서 cleaningType 문자열을 추출한다."""
    if status is None:
        return ""
    if hasattr(status, "cleaning_type") and getattr(status, "cleaning_type", None):
        return str(status.cleaning_type)
    raw = getattr(status, "raw_status", None) or (status if isinstance(status, dict) else {})
    if isinstance(raw, dict):
        main = raw.get("components", {}).get("main", {}) if isinstance(raw.get("components"), dict) else {}
        ct_dict = main.get("samsungce.robotCleanerCleaningType", {})
        if isinstance(ct_dict, dict):
            ct_val = ct_dict.get("cleaningType", {})
            return str(ct_val.get("value", "") if isinstance(ct_val, dict) else (ct_val or ""))
    return ""


def is_cleaner_in_session(status: Any) -> bool:
    """기기가 청소 또는 걸레 세척 등 청소 세션 상태인지 판별한다 (REQ-020, LB-059)."""
    if status is None:
        return False
    op_state = str(getattr(status, "operating_state", None) or (
        status.get("operating_state") if isinstance(status, dict) else ""
    )).lower()
    if op_state in SESSION_OPERATING_STATES:
        return True
    movement = str(getattr(status, "movement", None) or (
        status.get("movement") if isinstance(status, dict) else ""
    )).lower()
    if movement in ("cleaning", "washingmop"):
        return True
    return is_cleaner_cleaning(status)


def is_water_cleaning_session(status: Any, cleaning_type: str | None = None) -> bool:
    """물청소 세션인지 판별한다 (mop, vacuumAndMopTogether, mopAfterVacuum 또는 washingMop)."""
    op_state = str(getattr(status, "operating_state", None) or (
        status.get("operating_state") if isinstance(status, dict) else ""
    )).lower()
    movement = str(getattr(status, "movement", None) or (
        status.get("movement") if isinstance(status, dict) else ""
    )).lower()
    if op_state == "washingmop" or movement == "washingmop":
        return True

    ct = (cleaning_type or extract_cleaning_type(status)).lower()
    if ct in WATER_CLEANING_TYPES:
        return True
    if ct == CLEANING_TYPE_VACUUM.lower():
        return False
    return not ct


class WaterSessionTracker:
    """모든 경로(이 앱·스마트싱스·본체)의 물청소 세션 자동 감지 및 중복 방지 추적기 (REQ-020, LB-059)."""

    def __init__(
        self,
        storage: Storage,
        water_service: WaterService,
        client: SmartThingsClient | None = None,
        device_id: str = DEFAULT_DEVICE_ID,
        auto_stop_watcher: Any | None = None,
        now_func: Callable[[], datetime] = now_kst,
    ) -> None:
        self.storage = storage
        self.water_service = water_service
        self.client = client
        self.device_id = device_id
        self.auto_stop_watcher = auto_stop_watcher
        self.now_func = now_func
        self.logger: logging.Logger = logging.getLogger("server.water.session_tracker")
        self._is_in_session: bool = False
        self._session_counted: bool = False
        self._current_session_key: str | None = None

    @property
    def is_in_session(self) -> bool:
        return self._is_in_session

    @property
    def session_counted(self) -> bool:
        return self._session_counted

    @property
    def current_session_key(self) -> str | None:
        return self._current_session_key

    def reset(self) -> None:
        """세션 추적 내부 상태를 초기화한다."""
        self._is_in_session = False
        self._session_counted = False
        self._current_session_key = None

    def notify_reservation_success(
        self,
        reservation_id: str,
        cleaning_type: str | None = None,
        now: datetime | None = None,
    ) -> tuple[WaterTracker, bool] | None:
        """이 앱 1회성 예약 실행 성공 시 세션 상태를 동기화하고 물청소 횟수를 1회 카운트한다."""
        res_key = f"reservation:{reservation_id}"
        if self._current_session_key == res_key and self._session_counted:
            return None

        self._is_in_session = True
        self._session_counted = True
        self._current_session_key = res_key

        ct = cleaning_type or ""
        if ct == CLEANING_TYPE_VACUUM:
            self.logger.info("예약 먼지 단독 청소 성공 - 물청소 카운트 제외: res_id=%s", reservation_id)
            return None

        self.logger.info("예약 물청소 성공 - 물걸레 청소 횟수 1회 증가: res_id=%s", reservation_id)
        return self.water_service.record_mop_clean(is_mop=True)

    def is_autostop_target(self, now: datetime) -> bool:
        """현재 시각이 쉬는 기간 중 즉시 정지 대상(AutoStop) 청소인지 확인한다."""
        if self.auto_stop_watcher is not None and hasattr(self.auto_stop_watcher, "get_active_off_period"):
            op = self.auto_stop_watcher.get_active_off_period(now)
            if op is not None and hasattr(self.auto_stop_watcher, "is_allowed_reservation_cleaning"):
                if not self.auto_stop_watcher.is_allowed_reservation_cleaning(now):
                    return True
        return False

    def step(
        self,
        now: datetime | None = None,
        device_status: Any | None = None,
    ) -> tuple[WaterTracker, bool] | None:
        """기기 상태를 점검하여 외부 시작 물청소 세션을 자동 감지하고 1회 카운트한다."""
        current_now = now if now is not None else self.now_func()
        status = device_status
        if status is None and self.client is not None and hasattr(self.client, "get_device_status"):
            try:
                status = self.client.get_device_status(self.device_id)
            except Exception as e:
                self.logger.warning("물청소 세션 기기 상태 조회 실패: device_id=%s, error=%s", self.device_id, e)
                return None

        if status is None:
            return None

        in_session = is_cleaner_in_session(status)
        if not in_session:
            if self._is_in_session:
                self.logger.info("청소 세션 종료 감지: key=%s", self._current_session_key)
                self.reset()
            return None

        if self._is_in_session:
            return None

        self._is_in_session = True
        self._current_session_key = f"external:{format_kst(current_now)}"

        if self.is_autostop_target(current_now):
            self._session_counted = True
            self.logger.info("쉬는 기간 자동 정지 세션 감지 - 물청소 카운트 제외 (key=%s)", self._current_session_key)
            return None

        is_mop = is_water_cleaning_session(status)
        if not is_mop:
            self._session_counted = True
            self.logger.info("먼지 단독 세션 감지 - 물청소 카운트 제외 (key=%s)", self._current_session_key)
            return None

        self._session_counted = True
        self.logger.info("외부 시작 물청소 세션 감지 - 카운트 1회 증가 (key=%s)", self._current_session_key)
        return self.water_service.record_mop_clean(is_mop=True)
