from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from server.config import Config
from server.notify import (
    FCMClient,
    NotificationService,
    NotificationValidationError,
)
from server.server import MiniPCHandler, MiniPCServer, create_server
from server.storage import Storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android"


class MockSocket:
    """테스트용 인메모리 HTTP 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class TestNotifications(unittest.TestCase):
    """FCM 푸시 알림 및 다중 기기 발송 테스트 (LB-011, D-12, REQ-022, done_when)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_notifications.db"
        self.storage = Storage(self.db_path)
        self.fcm_client = FCMClient(simulated=True)
        self.service = NotificationService(self.storage, self.fcm_client)
        self.test_secret = "test-shared-secret-1234"

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def _request(
        self,
        server: MiniPCServer,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        secret: str | None = "test-shared-secret-1234",
    ) -> tuple[int, dict[str, Any]]:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
        if payload is not None:
            lines.append("Content-Type: application/json; charset=utf-8")
            lines.append(f"Content-Length: {len(body_bytes)}")
        if secret:
            lines.append(f"X-Shared-Secret: {secret}")
        raw = "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body_bytes
        sock = MockSocket(raw)
        MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
        output = sock.wfile.getvalue().decode("utf-8")
        status_line = output.splitlines()[0]
        status_code = int(status_line.split(" ")[1])
        body_str = output.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in output else ""
        data = json.loads(body_str) if body_str else {}
        return status_code, data

    def test_register_two_devices(self) -> None:
        """갤럭시 S21+와 갤럭시 폴드8 두 대 기기 토큰 등록 및 갱신 검증."""
        t1 = self.service.register_device("galaxy-s21-plus", "token-s21-alpha", target_id="nt-s21")
        t2 = self.service.register_device("galaxy-fold-8", "token-fold8-beta", target_id="nt-fold8")

        self.assertEqual(t1.device_id, "galaxy-s21-plus")
        self.assertEqual(t1.push_token, "token-s21-alpha")
        self.assertEqual(t2.device_id, "galaxy-fold-8")
        self.assertEqual(t2.push_token, "token-fold8-beta")

        targets = self.service.get_registered_targets()
        self.assertEqual(len(targets), 2)
        dev_ids = {t.device_id for t in targets}
        self.assertEqual(dev_ids, {"galaxy-s21-plus", "galaxy-fold-8"})

        # 토큰 갱신 (같은 device_id에 새로운 토큰 등록 시 덮어쓰기)
        updated = self.service.register_device("galaxy-s21-plus", "token-s21-new")
        self.assertEqual(updated.push_token, "token-s21-new")
        targets_after = self.service.get_registered_targets()
        self.assertEqual(len(targets_after), 2)
        s21 = self.storage.get_notification_target("galaxy-s21-plus")
        self.assertIsNotNone(s21)
        assert s21 is not None
        self.assertEqual(s21.push_token, "token-s21-new")

    def test_validation_error_on_empty_device_or_token(self) -> None:
        """빈 device_id 또는 빈 push_token 전달 시 검증 오류 발생."""
        with self.assertRaises(NotificationValidationError):
            self.service.register_device("", "some-token")
        with self.assertRaises(NotificationValidationError):
            self.service.register_device("galaxy-s21-plus", "   ")

    def test_multicast_dispatches_to_both_devices(self) -> None:
        """done_when: 시험 알림 발송 시 갤럭시 S21+와 폴드8 두 대 모두에 같은 알림이 전송된다 (D-12)."""
        self.service.register_device("galaxy-s21-plus", "token-s21-abc")
        self.service.register_device("galaxy-fold-8", "token-fold8-xyz")

        result = self.service.send_test_notification(
            title="[시험] 청소 시작 안내",
            body="갤럭시 S21+와 폴드8 동시 알림 시험입니다.",
        )

        self.assertEqual(result.total_targets, 2)
        self.assertEqual(result.success_count, 2)
        self.assertEqual(result.failure_count, 0)
        self.assertTrue(result.all_succeeded)

        # 발송된 메시지 기록 검증 (두 대 모두 같은 제목과 본문 수신)
        dispatched = self.fcm_client.dispatched_messages
        self.assertEqual(len(dispatched), 2)

        s21_msg = next((m for m in dispatched if m["device_id"] == "galaxy-s21-plus"), None)
        fold8_msg = next((m for m in dispatched if m["device_id"] == "galaxy-fold-8"), None)

        self.assertIsNotNone(s21_msg)
        self.assertIsNotNone(fold8_msg)
        assert s21_msg is not None
        assert fold8_msg is not None

        self.assertEqual(s21_msg["title"], fold8_msg["title"])
        self.assertEqual(s21_msg["body"], fold8_msg["body"])
        self.assertEqual(s21_msg["title"], "[시험] 청소 시작 안내")
        self.assertEqual(s21_msg["body"], "갤럭시 S21+와 폴드8 동시 알림 시험입니다.")
        self.assertEqual(s21_msg["push_token"], "token-s21-abc")
        self.assertEqual(fold8_msg["push_token"], "token-fold8-xyz")

    def test_server_notification_endpoints(self) -> None:
        """서버 REST API 엔드포인트(/api/notification-targets, /api/notify/test) 동작 검증."""
        server = create_server(
            Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.test_secret),
            notification_service=self.service,
        )
        try:
            # 1) 미인증 요청 거부 (REQ-024)
            st_unauth, _ = self._request(server, "POST", "/api/notify/test", secret=None)
            self.assertEqual(st_unauth, 401)

            # 2) S21+ 기기 등록
            st1, b1 = self._request(
                server,
                "POST",
                "/api/notification-targets",
                {"device_id": "galaxy-s21-plus", "push_token": "token-s21"},
            )
            self.assertEqual(st1, 201)
            self.assertEqual(b1["device_id"], "galaxy-s21-plus")

            # 3) 폴드8 기기 등록
            st2, b2 = self._request(
                server,
                "POST",
                "/api/notification-targets",
                {"device_id": "galaxy-fold-8", "push_token": "token-fold8"},
            )
            self.assertEqual(st2, 201)
            self.assertEqual(b2["device_id"], "galaxy-fold-8")

            # 4) 등록 목록 조회
            st_list, b_list = self._request(server, "GET", "/api/notification-targets")
            self.assertEqual(st_list, 200)
            self.assertEqual(len(b_list["notification_targets"]), 2)

            # 5) 시험 알림 발송 (/api/notify/test)
            st_test, b_test = self._request(
                server,
                "POST",
                "/api/notify/test",
                {"title": "서버 시험 푸시", "body": "두 폰에 정상 발송되는지 테스트합니다."},
            )
            self.assertEqual(st_test, 200)
            self.assertEqual(b_test["total_targets"], 2)
            self.assertEqual(b_test["success_count"], 2)
            self.assertTrue(b_test["all_succeeded"])

            # 6) 기기 삭제
            st_del, b_del = self._request(server, "DELETE", "/api/notification-targets/galaxy-s21-plus")
            self.assertEqual(st_del, 200)
            self.assertTrue(b_del["deleted"])
        finally:
            server.close()

    def test_gitignore_covers_firebase_secrets(self) -> None:
        """Firebase 인증 키 파일이 .gitignore에 포함되어 커밋 방지되는지 검증 (MUST)."""
        gitignore_path = PROJECT_ROOT / ".gitignore"
        self.assertTrue(gitignore_path.is_file())
        content = gitignore_path.read_text(encoding="utf-8")
        self.assertIn("serviceAccountKey", content)
        self.assertIn("google-services", content)
        self.assertIn("firebase", content)

    def test_android_manifest_and_code_contracts(self) -> None:
        """안드로이드 Manifest, Service, Client, Layout 계약 검증."""
        manifest_path = ANDROID_ROOT / "app/src/main/AndroidManifest.xml"
        self.assertTrue(manifest_path.is_file())
        manifest_text = manifest_path.read_text(encoding="utf-8")

        # 1. POST_NOTIFICATIONS 권한 선언
        self.assertIn("android.permission.POST_NOTIFICATIONS", manifest_text)

        # 2. AppFirebaseMessagingService 서비스 선언
        self.assertIn("AppFirebaseMessagingService", manifest_text)
        self.assertIn("com.google.firebase.MESSAGING_EVENT", manifest_text)

        # 3. Kotlin 파일 계약 검증
        service_file = (
            ANDROID_ROOT
            / "app/src/main/java/com/smartthings/cleaner/notification/AppFirebaseMessagingService.kt"
        )
        self.assertTrue(service_file.is_file())
        service_text = service_file.read_text(encoding="utf-8")
        self.assertIn("class AppFirebaseMessagingService", service_text)
        self.assertIn("fun onNewToken", service_text)
        self.assertIn("fun onMessageReceived", service_text)
        self.assertIn("cleaner_notifications", service_text)

        # 4. ServerClient 계약 검증
        client_file = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner/network/ServerClient.kt"
        client_text = client_file.read_text(encoding="utf-8")
        self.assertIn("fun registerNotificationTarget", client_text)
        self.assertIn("fun sendTestNotification", client_text)

        # 5. MainActivity & Layout 계약 검증
        # LB-047: 시험 알림 기능은 메인에서 설정 화면(SettingsActivity)으로 이동했다. 기능 존재 요구는 유지.
        main_file = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner/SettingsActivity.kt"
        main_text = main_file.read_text(encoding="utf-8")
        self.assertIn("btnSendTestNotification", main_text)
        self.assertIn("sendTestNotification", main_text)

        layout_file = ANDROID_ROOT / "app/src/main/res/layout/activity_settings.xml"
        layout_text = layout_file.read_text(encoding="utf-8")
        self.assertIn("btnSendTestNotification", layout_text)

        strings_file = ANDROID_ROOT / "app/src/main/res/values/strings.xml"
        strings_text = strings_file.read_text(encoding="utf-8")
        self.assertIn("btn_send_test_notification", strings_text)
