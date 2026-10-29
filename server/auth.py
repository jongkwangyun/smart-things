from __future__ import annotations

import hmac
from collections.abc import Mapping
from email.message import Message

HEALTH_CHECK_PATHS: frozenset[str] = frozenset(
    {"/", "/health", "/healthz", "/api/health"}
)

HeaderSource = Mapping[str, str] | Message


def is_health_check_path(path: str) -> bool:
    """상태 확인 주소인지 여부를 반환한다. 상태 확인 주소는 비밀키 인증에서 제외된다."""
    normalized = path.rstrip("/") or "/"
    return normalized in HEALTH_CHECK_PATHS


def extract_secret_from_headers(headers: HeaderSource | None) -> str | None:
    """HTTP 요청 헤더에서 공유 비밀키를 추출한다.

    지원 형식:
    1) X-Shared-Secret: <secret>
    2) Authorization: Bearer <secret>
    """
    if headers is None:
        return None

    # 1. X-Shared-Secret 헤더 확인 (대소문자 무관 탐색)
    for key, value in headers.items():
        if key.lower() == "x-shared-secret":
            val = value.strip()
            if val:
                return val

    # 2. Authorization: Bearer <secret> 헤더 확인
    for key, value in headers.items():
        if key.lower() == "authorization":
            parts = value.strip().split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                val = parts[1].strip()
                if val:
                    return val

    return None


def verify_shared_secret(provided_secret: str | None, expected_secret: str | None) -> bool:
    """공유 비밀키를 타이밍 공격에 안전한 상수 시간 비교(compare_digest)로 검증한다."""
    if not provided_secret or not expected_secret:
        return False
    provided = provided_secret.strip()
    expected = expected_secret.strip()
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)
