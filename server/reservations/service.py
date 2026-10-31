from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from server.models import STATUS_CANCELLED, STATUS_SCHEDULED, Reservation
from server.reservations.validator import (
    ReservationError,
    ReservationValidationError,
    validate_reservation_create_payload,
    validate_reservation_update_payload,
)
from server.storage import Storage
from server.time_utils import now_kst


class ReservationConflictError(ReservationError):
    """예약 ID 중복 시 발생하는 예외."""


class ReservationService:
    """1회성 청소 예약 비즈니스 로직 서비스."""

    def __init__(
        self,
        storage: Storage,
        now_func: Callable[[], datetime] | None = None,
    ) -> None:
        self.storage = storage
        self.now_func = now_func or now_kst

    def create_reservation(self, payload: dict[str, Any]) -> Reservation:
        """1회성 청소 예약을 검증하고 '예정' 상태로 생성·저장한다.

        - D-03: 모드·구역 선택 없음
        - REQ-001: 분 단위 미래 일시만 허용
        - 중복 ID 방지
        """
        validated = validate_reservation_create_payload(payload, now_func=self.now_func)

        res_id = validated["id"] or f"res-{uuid.uuid4().hex[:8]}"
        if self.storage.get_reservation(res_id) is not None:
            raise ReservationConflictError(f"이미 존재하는 예약 ID입니다: {res_id}")

        created_now = self.now_func()
        reservation = Reservation(
            id=res_id,
            reserved_at=validated["reserved_at"],
            status=STATUS_SCHEDULED,
            allow_during_off_period=validated["allow_during_off_period"],
            failure_reason=None,
            last_device_state=None,
            created_at=created_now,
            updated_at=created_now,
        )

        return self.storage.save_reservation(reservation)

    def update_reservation(self, res_id: str, payload: dict[str, Any]) -> Reservation | None:
        """예정 상태인 1회성 청소 예약의 일시를 수정한다 (REQ-003, Must rule).

        - D-09: 두 사용자 동일 권한으로 수정 가능
        - Must: 이미 실행된 예약(status != STATUS_SCHEDULED 또는 executed_at is not None)은 수정 불가
        - REQ-001: 분 단위 미래 일시만 허용 (과거 시각 불가)
        - 일시 수정 시 updated_at 갱신 및 storage 영구 저장
        """
        res = self.storage.get_reservation(res_id)
        if res is None:
            return None

        if res.status != STATUS_SCHEDULED or res.executed_at is not None:
            raise ReservationValidationError("이미 실행되었거나 완료/취소된 예약은 수정할 수 없습니다.")

        validated = validate_reservation_update_payload(payload, now_func=self.now_func)

        now = self.now_func()
        res.reserved_at = validated["reserved_at"]
        res.updated_at = now
        if "allow_during_off_period" in validated:
            res.allow_during_off_period = validated["allow_during_off_period"]

        return self.storage.save_reservation(res)

    def cancel_reservation(self, res_id: str) -> Reservation | None:
        """예정 상태인 1회성 청소 예약을 취소한다 (REQ-004, Must rule).

        - D-09: 두 사용자 동일 권한으로 취소 가능
        - Must: 이미 실행된 예약(status != STATUS_SCHEDULED 또는 executed_at is not None)은 취소 불가
        - 취소 시 status를 STATUS_CANCELLED로 변경하고 updated_at 갱신 및 storage 영구 저장
        """
        res = self.storage.get_reservation(res_id)
        if res is None:
            return None

        if res.status != STATUS_SCHEDULED or res.executed_at is not None:
            raise ReservationValidationError("이미 실행되었거나 완료/취소된 예약은 취소할 수 없습니다.")

        now = self.now_func()
        res.status = STATUS_CANCELLED
        res.updated_at = now
        return self.storage.save_reservation(res)

    def list_reservations(self) -> list[Reservation]:
        """모든 예약 목록을 반환한다."""
        return self.storage.list_reservations()

    def get_reservation(self, res_id: str) -> Reservation | None:
        """지정된 ID의 예약을 반환한다."""
        return self.storage.get_reservation(res_id)

    def delete_reservation(self, res_id: str) -> bool:
        """지정된 ID의 예약을 삭제한다."""
        return self.storage.delete_reservation(res_id)
