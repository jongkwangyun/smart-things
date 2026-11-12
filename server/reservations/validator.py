from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from server.models import DEFAULT_CLEANING_TYPE, VALID_CLEANING_TYPES
from server.time_utils import format_kst, is_minute_precision, now_kst, parse_kst

# D-03: 청소 모드·구역 선택 없음, 기본 전체 청소
FORBIDDEN_MODE_ZONE_KEYS: frozenset[str] = frozenset(
    {
        "mode",
        "cleaning_mode",
        "zone",
        "zones",
        "cleaning_zone",
        "room",
        "rooms",
        "area",
        "areas",
    }
)


class ReservationError(Exception):
    """예약 도메인 기본 예외."""


class ReservationValidationError(ReservationError):
    """예약 요청 검증 실패 예외."""


def validate_reservation_create_payload(
    payload: Any,
    now_func: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """1회성 예약 생성 요청 본문을 검증하고 정규화된 딕셔너리를 반환한다.

    규칙:
    1. 요청 본문은 JSON 객체(dict)여야 한다.
    2. D-03: 청소 모드나 구역 선택 필드가 포함되어 있으면 거부한다.
    3. scheduled_at 또는 reserved_at 필드로 일시를 지정해야 한다.
    4. 일시는 KST(+09:00) 기준 유효한 ISO 8601 문자열이어야 한다.
    5. 분 단위 정밀도만 허용한다(초/마이크로초=0).
    6. REQ-001: 현재 KST 시각보다 미래여야 한다 (현재 또는 과거는 거부).
    7. allow_during_off_period 필드가 있는 경우 boolean이어야 한다.
    """
    if not isinstance(payload, dict):
        raise ReservationValidationError("요청 본문은 JSON 객체여야 합니다.")

    # 1. D-03: 청소 모드 및 구역 선택 거부
    forbidden_present = [k for k in FORBIDDEN_MODE_ZONE_KEYS if k in payload]
    if forbidden_present:
        fields = ", ".join(sorted(forbidden_present))
        raise ReservationValidationError(
            f"청소 모드 및 구역 선택은 지원되지 않습니다 (D-03: 기본 전체 청소만 지원). "
            f"제공된 금지 필드: {fields}"
        )

    # 2. scheduled_at 또는 reserved_at 필드 확인
    raw_datetime = payload.get("scheduled_at")
    if raw_datetime is None:
        raw_datetime = payload.get("reserved_at")

    if raw_datetime is None:
        raise ReservationValidationError("'scheduled_at' 또는 'reserved_at' 필드가 필요합니다.")

    if not isinstance(raw_datetime, str) or not raw_datetime.strip():
        raise ReservationValidationError("예약 일시는 비어있지 않은 ISO 8601 문자열이어야 합니다.")

    # 3. KST datetime 파싱
    try:
        dt = parse_kst(raw_datetime)
    except (ValueError, TypeError) as err:
        raise ReservationValidationError(f"유효한 ISO 8601 일시 형식이 아닙니다: {raw_datetime}") from err

    # 4. must: 분 단위 일시만 받는다 (초, 마이크로초 0 필수)
    if not is_minute_precision(dt):
        raise ReservationValidationError(
            f"예약 시각은 분 단위여야 합니다 (초/마이크로초는 0이어야 함): {raw_datetime}"
        )

    # 5. REQ-001: 현재 KST 시각보다 미래여야 한다
    curr_now = (now_func or now_kst)()
    if dt <= curr_now:
        raise ReservationValidationError(
            f"예약 시각은 현재 시각({format_kst(curr_now)})보다 미래여야 합니다: {format_kst(dt)}"
        )

    # 6. id 검증 (선택)
    res_id: str | None = None
    if "id" in payload:
        raw_id = payload["id"]
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ReservationValidationError("예약 ID는 비어있지 않은 문자열이어야 합니다.")
        res_id = raw_id.strip()

    # 7. allow_during_off_period 검증 (선택, 기본 False)
    allow_off = payload.get("allow_during_off_period", False)
    if not isinstance(allow_off, bool):
        raise ReservationValidationError("'allow_during_off_period' 필드는 boolean이어야 합니다.")

    # 8. cleaning_type 검증 (선택, 기본 mopAfterVacuum)
    cleaning_type = payload.get("cleaning_type", DEFAULT_CLEANING_TYPE)
    if not isinstance(cleaning_type, str) or cleaning_type not in VALID_CLEANING_TYPES:
        allowed = ", ".join(sorted(VALID_CLEANING_TYPES))
        raise ReservationValidationError(
            f"유효하지 않은 청소 방식입니다: {cleaning_type}. 허용 값: {allowed}"
        )

    return {
        "id": res_id,
        "reserved_at": dt,
        "allow_during_off_period": allow_off,
        "cleaning_type": cleaning_type,
    }


def validate_reservation_update_payload(
    payload: Any,
    now_func: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """1회성 예약 수정 요청 본문을 검증하고 정규화된 딕셔너리를 반환한다 (REQ-003, REQ-001).

    규칙:
    1. 요청 본문은 JSON 객체(dict)여야 한다.
    2. D-03: 청소 모드나 구역 선택 필드가 포함되어 있으면 거부한다.
    3. scheduled_at 또는 reserved_at 필드로 새 일시를 지정해야 한다.
    4. 일시는 KST(+09:00) 기준 유효한 ISO 8601 문자열이어야 한다.
    5. 분 단위 정밀도만 허용한다(초/마이크로초=0).
    6. REQ-001: 현재 KST 시각보다 미래여야 한다 (과거 또는 현재는 거부).
    7. allow_during_off_period 필드가 있는 경우 boolean이어야 한다.
    """
    if not isinstance(payload, dict):
        raise ReservationValidationError("요청 본문은 JSON 객체여야 합니다.")

    forbidden_present = [k for k in FORBIDDEN_MODE_ZONE_KEYS if k in payload]
    if forbidden_present:
        fields = ", ".join(sorted(forbidden_present))
        raise ReservationValidationError(
            f"청소 모드 및 구역 선택은 지원되지 않습니다 (D-03: 기본 전체 청소만 지원). "
            f"제공된 금지 필드: {fields}"
        )

    raw_datetime = payload.get("scheduled_at")
    if raw_datetime is None:
        raw_datetime = payload.get("reserved_at")

    if raw_datetime is None:
        raise ReservationValidationError("'scheduled_at' 또는 'reserved_at' 필드가 필요합니다.")

    if not isinstance(raw_datetime, str) or not raw_datetime.strip():
        raise ReservationValidationError("예약 일시는 비어있지 않은 ISO 8601 문자열이어야 합니다.")

    try:
        dt = parse_kst(raw_datetime)
    except (ValueError, TypeError) as err:
        raise ReservationValidationError(f"유효한 ISO 8601 일시 형식이 아닙니다: {raw_datetime}") from err

    if not is_minute_precision(dt):
        raise ReservationValidationError(
            f"예약 시각은 분 단위여야 합니다 (초/마이크로초는 0이어야 함): {raw_datetime}"
        )

    curr_now = (now_func or now_kst)()
    if dt <= curr_now:
        raise ReservationValidationError(
            f"예약 시각은 현재 시각({format_kst(curr_now)})보다 미래여야 합니다: {format_kst(dt)}"
        )

    result: dict[str, Any] = {"reserved_at": dt}
    if "allow_during_off_period" in payload:
        allow_off = payload["allow_during_off_period"]
        if not isinstance(allow_off, bool):
            raise ReservationValidationError("'allow_during_off_period' 필드는 boolean이어야 합니다.")
        result["allow_during_off_period"] = allow_off

    if "cleaning_type" in payload:
        cleaning_type = payload["cleaning_type"]
        if not isinstance(cleaning_type, str) or cleaning_type not in VALID_CLEANING_TYPES:
            allowed = ", ".join(sorted(VALID_CLEANING_TYPES))
            raise ReservationValidationError(
                f"유효하지 않은 청소 방식입니다: {cleaning_type}. 허용 값: {allowed}"
            )
        result["cleaning_type"] = cleaning_type

    return result

