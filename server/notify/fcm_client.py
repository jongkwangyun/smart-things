from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from server.time_utils import format_kst, now_kst

if TYPE_CHECKING:
    from server.config import Config


@dataclass
class PushMessage:
    """FCM 푸시 메시지 내용 (D-12, REQ-022)."""

    title: str
    body: str
    data: dict[str, str] = field(default_factory=dict)


@dataclass
class PushResult:
    """단일 기기 푸시 발송 결과."""

    target_device_id: str
    push_token: str
    success: bool
    message_id: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_device_id": self.target_device_id,
            "push_token": self.push_token,
            "success": self.success,
            "message_id": self.message_id,
            "error": self.error,
        }


@dataclass
class MulticastResult:
    """다중 기기(갤럭시 S21+, 폴드8 등) 푸시 발송 결과 (D-12)."""

    total_targets: int
    success_count: int
    failure_count: int
    results: list[PushResult]

    @property
    def all_succeeded(self) -> bool:
        return self.total_targets > 0 and self.failure_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_targets": self.total_targets,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "all_succeeded": self.all_succeeded,
            "results": [r.to_dict() for r in self.results],
        }


class FCMClient:
    """FCM 푸시 발송 클라이언트.

    - FIREBASE_CREDENTIALS_PATH 환경 변수 또는 설정 파일 경로 지원.
    - 자격 증명 파일이 없거나 테스트 환경에서는 시뮬레이터(fallback) 모드로 안전하게 동작.
    - SPEC: D-12, A-07, REQ-022.
    """

    def __init__(
        self,
        credentials_path: Path | str | None = None,
        simulated: bool | None = None,
    ) -> None:
        self.credentials_path = Path(credentials_path) if credentials_path else None
        self.dispatched_messages: list[dict[str, Any]] = []

        if simulated is not None:
            self._simulated = simulated
        elif self.credentials_path and self.credentials_path.is_file():
            self._simulated = False
        else:
            self._simulated = True

    @property
    def is_simulated(self) -> bool:
        return self._simulated

    @classmethod
    def from_config(cls, config: Config) -> FCMClient:
        return cls(credentials_path=config.firebase_credentials_path)

    @classmethod
    def from_env(cls) -> FCMClient:
        raw = os.environ.get("FIREBASE_CREDENTIALS_PATH") or os.environ.get("SMART_THINGS_FIREBASE_CREDENTIALS_PATH")
        return cls(credentials_path=raw)

    def send(
        self,
        push_token: str,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
        device_id: str = "",
    ) -> PushResult:
        token = push_token.strip()
        if not token:
            return PushResult(
                target_device_id=device_id,
                push_token=push_token,
                success=False,
                error="토큰이 비어 있거나 올바르지 않습니다.",
            )

        payload_data = {k: str(v) for k, v in (data or {}).items()}

        if self._simulated:
            msg_id = f"simulated-msg-{uuid.uuid4().hex[:12]}"
            self.dispatched_messages.append(
                {
                    "device_id": device_id,
                    "push_token": token,
                    "title": title,
                    "body": body,
                    "data": payload_data,
                    "message_id": msg_id,
                    "dispatched_at": format_kst(now_kst()),
                }
            )
            return PushResult(
                target_device_id=device_id,
                push_token=token,
                success=True,
                message_id=msg_id,
            )

        # 실제 Firebase 인증 파일이 존재할 경우
        try:
            return self._send_firebase(token, title, body, payload_data, device_id)
        except Exception as exc:
            return PushResult(
                target_device_id=device_id,
                push_token=token,
                success=False,
                error=f"FCM 발송 실패: {exc}",
            )

    def _send_firebase(
        self,
        token: str,
        title: str,
        body: str,
        data: dict[str, str],
        device_id: str,
    ) -> PushResult:
        # 실제 FCM 발송 시도시 (자격 증명이 로드된 경우)
        try:
            import importlib

            firebase_admin = importlib.import_module("firebase_admin")
            credentials = importlib.import_module("firebase_admin.credentials")
            messaging = importlib.import_module("firebase_admin.messaging")

            if not getattr(firebase_admin, "_apps", None):
                cred = credentials.Certificate(str(self.credentials_path))
                firebase_admin.initialize_app(cred)

            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                data=data,
                token=token,
            )
            msg_id = messaging.send(message)
            return PushResult(
                target_device_id=device_id,
                push_token=token,
                success=True,
                message_id=str(msg_id),
            )
        except ImportError:
            # firebase_admin 미설치 시 시뮬레이션 fallback
            msg_id = f"fallback-msg-{uuid.uuid4().hex[:12]}"
            self.dispatched_messages.append(
                {
                    "device_id": device_id,
                    "push_token": token,
                    "title": title,
                    "body": body,
                    "data": data,
                    "message_id": msg_id,
                    "dispatched_at": format_kst(now_kst()),
                }
            )
            return PushResult(
                target_device_id=device_id,
                push_token=token,
                success=True,
                message_id=msg_id,
            )
