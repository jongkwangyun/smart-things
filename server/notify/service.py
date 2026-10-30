from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from server.models import NotificationTarget
from server.notify.fcm_client import FCMClient, MulticastResult, PushResult
from server.time_utils import now_kst

if TYPE_CHECKING:
    from server.storage import Storage


class NotificationValidationError(ValueError):
    """알림 대상 유효성 검증 실패 예외."""


class NotificationService:
    """푸시 알림 서비스 (D-12, REQ-022).

    - SPEC:
      * D-12: 알림 채널은 앱 푸시(FCM), 두 사용자 모두 수신 (갤럭시 S21+, 폴드8).
      * done_when: 서버에서 시험 알림을 한 번 보내면 두 대 모두의 알림창에 같은 알림이 뜬다.
      * must: 모든 알림은 등록된 두 기기 모두에 보낸다.
    """

    def __init__(self, storage: Storage, fcm_client: FCMClient | None = None) -> None:
        self.storage = storage
        self.fcm_client = fcm_client or FCMClient.from_env()

    def register_device(
        self,
        device_id: str,
        push_token: str,
        target_id: str | None = None,
    ) -> NotificationTarget:
        """기기 식별자 및 푸시 토큰 등록/갱신."""
        clean_device = str(device_id).strip()
        clean_token = str(push_token).strip()

        if not clean_device:
            raise NotificationValidationError("기기 식별자(device_id)는 필수입니다.")
        if not clean_token:
            raise NotificationValidationError("푸시 토큰(push_token)은 필수입니다.")

        target = NotificationTarget(
            id=str(target_id).strip() if target_id else uuid.uuid4().hex[:8],
            device_id=clean_device,
            push_token=clean_token,
            updated_at=now_kst(),
        )
        return self.storage.save_notification_target(target)

    def get_registered_targets(self) -> list[NotificationTarget]:
        """등록된 모든 푸시 알림 대상 기기 목록 조회."""
        return self.storage.list_notification_targets()

    def send_multicast(
        self,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> MulticastResult:
        """등록된 모든 기기(두 대: S21+, 폴드8)로 알림 브로드캐스트 (D-12)."""
        targets = self.storage.list_notification_targets()
        results: list[PushResult] = []

        for target in targets:
            res = self.fcm_client.send(
                push_token=target.push_token,
                title=title,
                body=body,
                data=data,
                device_id=target.device_id,
            )
            results.append(res)

        success_count = sum(1 for r in results if r.success)
        failure_count = len(results) - success_count
        return MulticastResult(
            total_targets=len(targets),
            success_count=success_count,
            failure_count=failure_count,
            results=results,
        )

    def send_test_notification(
        self,
        title: str = "청소기 시험 알림",
        body: str = "시험 푸시 알림입니다. 갤럭시 S21+와 갤럭시 폴드8 두 대 모두 정상 수신되었습니다.",
    ) -> MulticastResult:
        """시험 알림 발송 (done_when 기준)."""
        return self.send_multicast(
            title=title,
            body=body,
            data={"type": "test_notification", "source": "smart-things-mini-pc"},
        )
