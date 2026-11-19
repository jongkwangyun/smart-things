from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.off_periods.auto_stop import DEFAULT_DEVICE_ID
from server.smartthings.auth import TokenRefreshError
from server.smartthings.client import SmartThingsAuthError
from server.time_utils import to_kst

if TYPE_CHECKING:
    from server.models import OffPeriod
    from server.smartthings.client import SmartThingsClient
    from server.storage import Storage


def _extract_dnd_from_status(device_status: Any) -> dict[str, str] | None:
    """이미 조회된 DeviceStatus.raw_status 에서 DND 설정을 추출한다."""
    if device_status is None:
        return None
    raw = getattr(device_status, "raw_status", None)
    if not isinstance(raw, dict):
        return None
    dnd = raw.get("components", {}).get("main", {}).get("custom.doNotDisturbMode")
    if not isinstance(dnd, dict) or not dnd:
        return None
    return {
        "startTime": str(dnd.get("startTime", {}).get("value", "0000")),
        "endTime": str(dnd.get("endTime", {}).get("value", "0000")),
        "doNotDisturb": str(dnd.get("doNotDisturb", {}).get("value", "off")),
    }


@dataclass(frozen=True)
class DndSyncOutcome:
    """단일 DndSyncManager.sync() 호출의 불변 실행 결과."""

    changed: bool = False
    succeeded: bool = False
    failed: bool = False
    auth_error: Exception | None = None
    error_message: str | None = None


class DndSyncManager:
    """쉬는 기간 중 방해금지 모드(DND) 자동 활성화 및 해제 매니저 (REQ-016, LB-045).

    - 쉬는 기간 시작 시: 기기 DND 현재 설정을 영구 저장소에 백업하고, 종일(0000~2359) DND on 활성화.
    - 쉬는 기간 종료 또는 삭제 시: 영구 저장소에서 백업을 읽어 기기 DND 설정을 원래대로 복원하고 백업 삭제.
    - 장애 방어: API 오류 등 예외 발생 시 로깅 후 안전하게 종료하여 스케줄러 루프 중단을 방지함.
    """

    def __init__(
        self,
        storage: Storage,
        client: SmartThingsClient,
        device_id: str = DEFAULT_DEVICE_ID,
        logger: logging.Logger | None = None,
    ) -> None:
        self.storage = storage
        self.client = client
        self.device_id = device_id
        self.logger = logger or logging.getLogger("server.off_periods.dnd_sync")
        self._lock = threading.RLock()
        self.last_auth_error: Exception | None = None
        self.last_error: str | None = None
        self.last_sync_failed: bool = False
        self.last_sync_succeeded: bool = False

    def get_active_off_period(self, now: datetime) -> OffPeriod | None:
        """지정 시각이 포함된 활성 쉬는 기간을 반환한다."""
        now_k = to_kst(now)
        for op in self.storage.list_off_periods():
            if to_kst(op.start_at) <= now_k <= to_kst(op.end_at):
                return op
        return None

    def needs_device_status(self, now: datetime) -> bool:
        """현재 DND 동기화에 기기 상태 조회가 필요한지 반환한다."""
        with self._lock:
            return self.get_active_off_period(now) is not None and self.storage.get_dnd_backup() is None

    def _record_outcome(self, outcome: DndSyncOutcome) -> DndSyncOutcome:
        self.last_auth_error = outcome.auth_error
        self.last_error = outcome.error_message
        self.last_sync_failed = outcome.failed
        self.last_sync_succeeded = outcome.succeeded
        return outcome

    def sync_with_outcome(self, now: datetime, device_status: Any | None = None) -> DndSyncOutcome:
        """현재 시각 기준으로 DND를 동기화하고 호출별 불변 DndSyncOutcome을 반환한다."""
        with self._lock:
            if not hasattr(self.client, "get_dnd_status") or not hasattr(self.client, "set_dnd_mode"):
                return self._record_outcome(DndSyncOutcome())
            try:
                active_op = self.get_active_off_period(now)
                backup: dict[str, Any] | None = self.storage.get_dnd_backup()

                # 1. 활성 쉬는 기간 진입: 기존 DND 백업 후 종일 DND 활성화
                if active_op is not None and backup is None:
                    curr = _extract_dnd_from_status(device_status) or self.client.get_dnd_status(self.device_id)
                    self.storage.save_dnd_backup(curr)
                    self.logger.info("쉬는 기간 진입으로 DND 백업 완료 (op_id=%s, backup=%s)", active_op.id, curr)
                    curr_mode = str(curr.get("doNotDisturb") or curr.get("mode") or "off").lower()
                    curr_start = str(curr.get("startTime") or curr.get("start_time") or "")
                    curr_end = str(curr.get("endTime") or curr.get("end_time") or "")
                    if not (curr_mode == "on" and curr_start == "0000" and curr_end == "2359"):
                        self.client.set_dnd_mode(self.device_id, "0000", "2359", "on")
                        self.logger.info("기기 방해금지 모드 종일 활성화 완료 (0000-2359, on)")
                    return self._record_outcome(DndSyncOutcome(changed=True, succeeded=True))

                # 2. 쉬는 기간 종료 또는 삭제: 백업된 원래 DND 설정으로 복원
                if active_op is None and backup is not None:
                    s_time = str(backup.get("startTime") or backup.get("start_time") or "0000")
                    e_time = str(backup.get("endTime") or backup.get("end_time") or "0000")
                    mode = str(backup.get("doNotDisturb") or backup.get("mode") or "off")
                    self.logger.info("쉬는 기간 종료: 원래 DND 설정으로 복원 시도 (%s-%s, %s)", s_time, e_time, mode)
                    self.client.set_dnd_mode(self.device_id, s_time, e_time, mode)
                    self.storage.clear_dnd_backup()
                    self.logger.info("원래 DND 설정 복원 및 백업 삭제 완료")
                    return self._record_outcome(DndSyncOutcome(changed=True, succeeded=True))

                return self._record_outcome(DndSyncOutcome())
            except (SmartThingsAuthError, TokenRefreshError) as e:
                self.logger.warning("DND 동기화 중 인증 오류 발생: %s", e)
                return self._record_outcome(DndSyncOutcome(failed=True, auth_error=e, error_message=str(e)))
            except Exception as e:
                self.logger.warning("DND 동기화 처리 중 오류 발생 (안전 무시): %s", e, exc_info=True)
                return self._record_outcome(DndSyncOutcome(failed=True, error_message=str(e)))

    def sync(self, now: datetime, device_status: Any | None = None) -> bool:
        """현재 시각 기준으로 쉬는 기간과 기기 DND 설정을 동기화한다."""
        return self.sync_with_outcome(now, device_status=device_status).changed
