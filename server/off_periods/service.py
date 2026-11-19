from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.models import OffPeriod
from server.off_periods.validator import (
    OffPeriodError,
    validate_off_period_payload,
)
from server.smartthings.auth import TokenRefreshError
from server.smartthings.client import SmartThingsAuthError
from server.storage import Storage
from server.time_utils import now_kst

if TYPE_CHECKING:
    from server.off_periods.dnd_sync import DndSyncManager


class OffPeriodConflictError(OffPeriodError):
    """쉬는 기간 ID 중복 시 발생하는 예외."""


class OffPeriodService:
    """쉬는 기간 비즈니스 로직 서비스 (REQ-011, SPEC F-3)."""

    def __init__(
        self,
        storage: Storage,
        now_func: Callable[[], datetime] | None = None,
        dnd_sync_manager: DndSyncManager | None = None,
        smartthings_health_getter: Callable[[], dict[str, Any]] | None = None,
        on_dnd_sync: Callable[..., None] | None = None,
    ) -> None:
        self.storage = storage
        self.now_func = now_func or now_kst
        self.dnd_sync_manager = dnd_sync_manager
        self.smartthings_health_getter = smartthings_health_getter
        self.on_dnd_sync = on_dnd_sync
        self.last_warning: str | None = None

    def _run_dnd_sync(self, now: datetime) -> tuple[Exception | None, Exception | None, bool, str | None]:
        """DND 동기화를 수행하고 호출별 결과 및 예외 정보를 반환한다."""
        dnd_m = self.dnd_sync_manager
        if dnd_m is None:
            return None, None, False, None
        sync_exc: Exception | None = None
        outcome: Any = None
        try:
            sync_with_outcome = getattr(dnd_m, "sync_with_outcome", None)
            if callable(sync_with_outcome):
                outcome = sync_with_outcome(now)
            else:
                dnd_m.sync(now)
        except Exception as exc:
            sync_exc = exc

        if outcome is not None:
            sync_auth_err = getattr(outcome, "auth_error", None)
            sync_failed = bool(getattr(outcome, "failed", False))
            sync_err_msg = getattr(outcome, "error_message", None)
        else:
            sync_auth_err = getattr(dnd_m, "last_auth_error", None)
            sync_failed = bool(getattr(dnd_m, "last_sync_failed", False))
            sync_err_msg = getattr(dnd_m, "last_error", None)

        if self.on_dnd_sync is not None:
            self.on_dnd_sync(dnd_m, now, sync_exc, outcome)
        return sync_exc, sync_auth_err, sync_failed, sync_err_msg

    def _sync_dnd_and_compute_warning(self, now: datetime) -> str | None:
        """DND 동기화를 수행하고 동기화 또는 스마트싱스 인증 실패 시 경고 메시지를 반환한다."""
        sync_exc, sync_auth_err, sync_failed, sync_err_msg = self._run_dnd_sync(now)

        auth_err: Exception | None = (
            sync_exc if isinstance(sync_exc, (SmartThingsAuthError, TokenRefreshError))
            else (sync_auth_err if isinstance(sync_auth_err, Exception) else None)
        )
        if auth_err is not None:
            return (
                "쉬는 기간은 저장되었으나 스마트싱스 인증 실패로 방해금지 모드(DND) "
                f"동기화를 완료하지 못했습니다: {auth_err}"
            )
        if sync_exc is not None or sync_failed:
            detail = str(sync_exc) if sync_exc is not None else (sync_err_msg or "연동 오류")
            return f"쉬는 기간은 저장되었으나 방해금지 모드(DND) 동기화에 실패했습니다: {detail}"
        if self.smartthings_health_getter is not None:
            st_health = self.smartthings_health_getter()
            if not st_health.get("smartthings_ok", True):
                detail = st_health.get("last_error") or st_health.get("smartthings_auth_state") or "인증 오류"
                if st_health.get("smartthings_auth_state") == "auth_failed":
                    return (
                        f"쉬는 기간은 저장되었으나 스마트싱스 인증이 실패 상태입니다({detail}). "
                        "재인증 전까지 DND 동기화와 자동 정지가 동작하지 않을 수 있습니다."
                    )
                return (
                    f"쉬는 기간은 저장되었으나 스마트싱스 연동 오류가 감지되었습니다({detail}). "
                    "DND 동기화 및 자동 정지가 원활하지 않을 수 있습니다."
                )
        return None

    def create_off_period(
        self_or_storage: Any,
        payload_or_data: dict[str, Any] | None = None,
    ) -> OffPeriod:
        """쉬는 기간을 검증하고 생성·저장한다 (REQ-011).

        인스턴스 호출: `OffPeriodService(storage).create_off_period(payload)`
        클래스/함수형 호출: `OffPeriodService.create_off_period(storage, payload)`
        두 방식을 모두 지원한다.
        """
        service_instance: OffPeriodService | None = None
        if isinstance(self_or_storage, OffPeriodService):
            service_instance = self_or_storage
            storage: Storage = self_or_storage.storage
            now_func = self_or_storage.now_func
            assert payload_or_data is not None
            payload: dict[str, Any] = payload_or_data
        else:
            storage = self_or_storage
            now_func = now_kst
            assert payload_or_data is not None
            payload = payload_or_data

        validated = validate_off_period_payload(payload)

        op_id = validated["id"] or f"off-{uuid.uuid4().hex[:8]}"
        if storage.get_off_period(op_id) is not None:
            raise OffPeriodConflictError(f"이미 존재하는 쉬는 기간 ID입니다: {op_id}")

        created_now = now_func()
        off_period = OffPeriod(
            id=op_id,
            start_at=validated["start_at"],
            end_at=validated["end_at"],
            created_at=created_now,
        )

        saved = storage.save_off_period(off_period)
        if service_instance is not None:
            service_instance.last_warning = service_instance._sync_dnd_and_compute_warning(created_now)
        return saved

    def create_off_period_response(self, payload: dict[str, Any]) -> dict[str, Any]:
        """쉬는 기간을 생성하고, 동기화/인증 경고 발생 시 warning 필드를 포함한 응답 딕셔너리를 반환한다."""
        saved = self.create_off_period(payload)
        resp = saved.to_dict()
        if self.last_warning:
            resp["warning"] = self.last_warning
        return resp

    def list_off_periods(self) -> list[OffPeriod]:
        """모든 쉬는 기간 목록을 반환한다."""
        return self.storage.list_off_periods()

    def get_off_period(self, op_id: str) -> OffPeriod | None:
        """지정된 ID의 쉬는 기간을 반환한다."""
        return self.storage.get_off_period(op_id)

    def delete_off_period(self, op_id: str) -> bool:
        """지정된 ID의 쉬는 기간을 삭제한다."""
        deleted = self.storage.delete_off_period(op_id)
        if deleted and self.dnd_sync_manager is not None:
            self._run_dnd_sync(self.now_func())
        return deleted

