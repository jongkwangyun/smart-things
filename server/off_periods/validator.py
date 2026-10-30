from __future__ import annotations

from typing import Any

from server.time_utils import is_minute_precision, parse_kst


class OffPeriodError(Exception):
    """쉬는 기간 도메인 기본 예외."""


class OffPeriodValidationError(OffPeriodError):
    """쉬는 기간 요청 검증 실패 예외."""


def validate_off_period_payload(payload: Any) -> dict[str, Any]:
    """쉬는 기간 등록 요청 본문을 검증하고 정규화된 딕셔너리를 반환한다 (REQ-011, LB-020).

    규칙:
    1. 요청 본문은 JSON 객체(dict)여야 한다.
    2. 'start_at'과 'end_at' 필드로 일시를 지정해야 한다.
    3. 일시는 KST(+09:00) 기준 유효한 ISO 8601 문자열이어야 한다.
    4. 분 단위 정밀도만 허용한다(초/마이크로초=0).
    5. REQ-011: 종료 일시는 시작 일시보다 뒤여야 한다 (end_at > start_at).
       종료가 시작보다 앞서거나 같으면 거부된다.
    """
    if not isinstance(payload, dict):
        raise OffPeriodValidationError("요청 본문은 JSON 객체여야 합니다.")

    if "start_at" not in payload or "end_at" not in payload:
        raise OffPeriodValidationError("'start_at'과 'end_at' 필드가 필요합니다.")

    raw_start = payload.get("start_at")
    raw_end = payload.get("end_at")

    if not isinstance(raw_start, str) or not raw_start.strip():
        raise OffPeriodValidationError("시작 일시는 비어있지 않은 ISO 8601 문자열이어야 합니다.")
    if not isinstance(raw_end, str) or not raw_end.strip():
        raise OffPeriodValidationError("종료 일시는 비어있지 않은 ISO 8601 문자열이어야 합니다.")

    try:
        start_dt = parse_kst(raw_start)
    except (ValueError, TypeError) as err:
        raise OffPeriodValidationError(f"시작 일시가 유효한 ISO 8601 일시 형식이 아닙니다: {raw_start}") from err

    try:
        end_dt = parse_kst(raw_end)
    except (ValueError, TypeError) as err:
        raise OffPeriodValidationError(f"종료 일시가 유효한 ISO 8601 일시 형식이 아닙니다: {raw_end}") from err

    if not is_minute_precision(start_dt):
        raise OffPeriodValidationError(
            f"시작 일시는 분 단위여야 합니다 (초/마이크로초는 0이어야 함): {raw_start}"
        )
    if not is_minute_precision(end_dt):
        raise OffPeriodValidationError(
            f"종료 일시는 분 단위여야 합니다 (초/마이크로초는 0이어야 함): {raw_end}"
        )

    # REQ-011: 종료가 시작보다 앞서면 등록할 수 없어야 한다 (end_at > start_at 필수)
    if end_dt <= start_dt:
        raise OffPeriodValidationError("종료 일시는 시작 일시보다 뒤여야 합니다.")

    op_id: str | None = None
    if "id" in payload:
        raw_id = payload["id"]
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise OffPeriodValidationError("쉬는 기간 ID는 비어있지 않은 문자열이어야 합니다.")
        op_id = raw_id.strip()

    return {
        "id": op_id,
        "start_at": start_dt,
        "end_at": end_dt,
    }
