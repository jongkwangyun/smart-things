from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from server.models import OffPeriod
from server.off_periods.validator import (
    OffPeriodError,
    validate_off_period_payload,
)
from server.storage import Storage
from server.time_utils import now_kst


class OffPeriodConflictError(OffPeriodError):
    """쉬는 기간 ID 중복 시 발생하는 예외."""


class OffPeriodService:
    """쉬는 기간 비즈니스 로직 서비스 (REQ-011, SPEC F-3)."""

    def __init__(
        self,
        storage: Storage,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.now_func = now_func or now_kst

    def create_off_period(
        self_or_storage: Any,
        payload_or_data: dict[str, Any] | None = None,
    ) -> OffPeriod:
        """쉬는 기간을 검증하고 생성·저장한다 (REQ-011).

        인스턴스 호출: `OffPeriodService(storage).create_off_period(payload)`
        클래스/함수형 호출: `OffPeriodService.create_off_period(storage, payload)`
        두 방식을 모두 지원한다.
        """
        if isinstance(self_or_storage, OffPeriodService):
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

        return storage.save_off_period(off_period)

    def list_off_periods(self) -> list[OffPeriod]:
        """모든 쉬는 기간 목록을 반환한다."""
        return self.storage.list_off_periods()

    def get_off_period(self, op_id: str) -> OffPeriod | None:
        """지정된 ID의 쉬는 기간을 반환한다."""
        return self.storage.get_off_period(op_id)

    def delete_off_period(self, op_id: str) -> bool:
        """지정된 ID의 쉬는 기간을 삭제한다."""
        return self.storage.delete_off_period(op_id)
