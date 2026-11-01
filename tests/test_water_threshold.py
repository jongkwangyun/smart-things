from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from server.config import Config
from server.notify import FCMClient, NotificationService
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage
from server.water.service import WaterService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android"
RES_LAYOUT = ANDROID_ROOT / "app/src/main/res/layout"
RES_VALUES = ANDROID_ROOT / "app/src/main/res/values"
SRC_JAVA = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"


class MockSocket:
    """테스트용 인메모리 HTTP 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


def simulate_http_request(
    server: MiniPCServer, method: str, path: str, secret: str | None = None, body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    headers = ["Host: 127.0.0.1"]
    if secret:
        headers.append(f"X-Shared-Secret: {secret}")
        headers.append(f"Authorization: Bearer {secret}")
    payload_bytes = b""
    if body is not None:
        payload_bytes = json.dumps(body).encode("utf-8")
        headers.append(f"Content-Length: {len(payload_bytes)}")
        headers.append("Content-Type: application/json")
    raw = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n" + payload_bytes.decode("utf-8")
    sock = MockSocket(raw.encode("utf-8"))
    MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
    parts = sock.wfile.getvalue().split(b"\r\n\r\n", 1)
    status_code = int(parts[0].split(b"\r\n")[0].decode("utf-8").split(" ")[1])
    body_data = json.loads(parts[1].decode("utf-8")) if len(parts) > 1 and parts[1] else {}
    return status_code, body_data


class TestWaterThreshold(unittest.TestCase):
    """LB-040: 알림 기준 횟수 N 변경 화면 및 동작 검증 (REQ-021, D-06, D-14)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_threshold.db"
        self.storage = Storage(self.db_path)
        self.secret = "secret-key-1234"
        self.config = Config(host="127.0.0.1", port=0, shared_secret=self.secret, db_path=self.db_path)
        self.fcm_client = FCMClient(simulated=True)
        self.notify_service = NotificationService(self.storage, self.fcm_client)
        self.notify_service.register_device("galaxy-s21-plus", "token-s21", target_id="t-s21")
        self.notify_service.register_device("galaxy-fold8", "token-fold8", target_id="t-fold8")
        self.server = MiniPCServer(self.config, self.storage, notification_service=self.notify_service)
        self.water_service = WaterService(self.storage, notification_service=self.notify_service)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.temp_dir.cleanup()

    def test_file_line_counts_under_300(self) -> None:
        """모든 관련 파일이 300줄 이하인지 확인 (Rules & Constraints)."""
        checked_files = [
            PROJECT_ROOT / "server/server.py",
            SRC_JAVA / "network/ServerClient.kt",
            SRC_JAVA / "WaterManagementActivity.kt",
            RES_LAYOUT / "activity_water_management.xml",
            RES_VALUES / "strings.xml",
            Path(__file__),
        ]
        for p in checked_files:
            self.assertTrue(p.is_file(), f"파일 누락: {p}")
            lines = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 300, f"{p.name} 줄 수 초과 ({lines} > 300)")

    def test_server_update_threshold_valid(self) -> None:
        """POST /api/water-tracker로 N을 변경하면 200 반환 및 변경된 값이 저장된다 (REQ-021)."""
        status, body = simulate_http_request(
            self.server, "POST", "/api/water-tracker", secret=self.secret, body={"alert_threshold_n": 5}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["alert_threshold_n"], 5)

        # GET으로 조회해도 변경된 N(5) 유지 확인
        get_status, get_body = simulate_http_request(
            self.server, "GET", "/api/water-tracker", secret=self.secret
        )
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["alert_threshold_n"], 5)

    def test_server_must_rule_rejects_non_positive_and_invalid(self) -> None:
        """Must rule: 1 이상의 정수만 허용한다. 0, 음수, 문자열은 400 반환."""
        for invalid_n in [0, -1, -10]:
            status, body = simulate_http_request(
                self.server, "POST", "/api/water-tracker", secret=self.secret, body={"alert_threshold_n": invalid_n}
            )
            self.assertEqual(status, 400, f"N={invalid_n}은 거부되어야 함")
            self.assertIn("alert_threshold_n must be an integer >= 1", body.get("error", ""))

        # 유효하지 않은 문자열
        status_str, body_str = simulate_http_request(
            self.server, "POST", "/api/water-tracker", secret=self.secret, body={"alert_threshold_n": "invalid_abc"}
        )
        self.assertEqual(status_str, 400)
        self.assertIn("alert_threshold_n must be a valid integer", body_str.get("error", ""))

    def test_alert_fires_based_on_updated_threshold(self) -> None:
        """Done When: 값을 바꾸면 다음 알림이 바뀐 값 기준으로 온다."""
        # 기본 N=7 상태에서 N=3으로 변경
        status, body = simulate_http_request(
            self.server, "POST", "/api/water-tracker", secret=self.secret, body={"alert_threshold_n": 3}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["alert_threshold_n"], 3)

        # 1회, 2회 물걸레 청소 수행: 알림 발송되지 않음
        _, alert1 = self.water_service.record_mop_clean(is_mop=True)
        self.assertFalse(alert1)
        _, alert2 = self.water_service.record_mop_clean(is_mop=True)
        self.assertFalse(alert2)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 0)

        # 3회 물걸레 청소 수행: 바뀐 N(=3) 기준에 도달하여 알림 발송!
        tracker, alert3 = self.water_service.record_mop_clean(is_mop=True)
        self.assertTrue(alert3)
        self.assertEqual(tracker.mop_clean_count, 3)
        self.assertEqual(tracker.alert_threshold_n, 3)

        # 두 기기 모두 알림 수신 및 알림 내용에 바뀐 기준(3회) 포함 확인
        self.assertEqual(len(self.fcm_client.dispatched_messages), 2)
        for msg in self.fcm_client.dispatched_messages:
            self.assertIn("3회(기준: 3회)에 도달했습니다", msg["body"])

    def test_android_source_code_and_resources(self) -> None:
        """Android 소스 코드와 XML 리소스의 LB-040 요구사항 구현 검증."""
        activity_src = (SRC_JAVA / "WaterManagementActivity.kt").read_text(encoding="utf-8")
        client_src = (SRC_JAVA / "network/ServerClient.kt").read_text(encoding="utf-8")
        layout_xml = (RES_LAYOUT / "activity_water_management.xml").read_text(encoding="utf-8")
        strings_xml = (RES_VALUES / "strings.xml").read_text(encoding="utf-8")

        # 1. ServerClient.kt: updateAlertThreshold 및 N < 1 검증
        self.assertIn("fun updateAlertThreshold", client_src)
        self.assertIn("thresholdN < 1", client_src)
        self.assertIn("alert_threshold_n", client_src)

        # 2. WaterManagementActivity.kt: UI 바인딩, 다이얼로그, 유효성 검사
        self.assertIn("btnChangeThreshold", activity_src)
        self.assertIn("showChangeThresholdDialog", activity_src)
        self.assertIn("performUpdateThreshold", activity_src)
        self.assertIn("currentThresholdN", activity_src)
        # Must rule 검증 포함: 1 이상의 정수만 허용
        self.assertTrue(
            "nVal < 1" in activity_src or "newN < 1" in activity_src,
            "Activity에 1 이상의 정수 유효성 검사가 있어야 합니다."
        )

        # 3. 레이아웃과 문자열 리소스
        self.assertIn("btnChangeThreshold", layout_xml)
        self.assertIn("textAlertThreshold", layout_xml)
        self.assertIn("btn_change_threshold", strings_xml)
        self.assertIn("dialog_title_change_threshold", strings_xml)
        self.assertIn("dialog_msg_change_threshold", strings_xml)
        self.assertIn("msg_error_invalid_threshold", strings_xml)
        self.assertIn("msg_success_threshold_updated", strings_xml)
