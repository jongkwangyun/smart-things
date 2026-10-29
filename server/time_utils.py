from __future__ import annotations

from datetime import datetime, timedelta, timezone

# 한국 표준시 (KST, UTC+9)
KST = timezone(timedelta(hours=9), name="KST")


def now_kst() -> datetime:
    """현재 시각을 KST 기준으로 반환한다."""
    return datetime.now(KST)


def to_kst(dt: datetime) -> datetime:
    """임의의 datetime 객체를 KST 기준 datetime으로 변환한다."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=KST)
    return dt.astimezone(KST)


def format_kst(dt: datetime) -> str:
    """KST datetime 객체를 ISO 8601 문자열(+09:00 포함)로 직렬화한다."""
    return to_kst(dt).isoformat()


def parse_kst(raw: str) -> datetime:
    """ISO 8601 문자열을 읽어 KST datetime 객체로 변환한다."""
    parsed = datetime.fromisoformat(raw.strip())
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)
