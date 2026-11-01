from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import WaterTracker
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage
from server.water.service import WaterService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android"
RES_LAYOUT = ANDROID_ROOT / "app/src/main/res/layout"
RES_VALUES = ANDROID_ROOT / "app/src/main/res/values"
SRC_JAVA = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"
MANIFEST_PATH = ANDROID_ROOT / "app/src/main/AndroidManifest.xml"


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
    response_bytes = sock.wfile.getvalue()
    parts = response_bytes.split(b"\r\n\r\n", 1)
    status_line = parts[0].split(b"\r\n")[0].decode("utf-8")
    status_code = int(status_line.split(" ")[1])
    body_data = json.loads(parts[1].decode("utf-8")) if len(parts) > 1 and parts[1] else {}
    return status_code, body_data


class TestWaterManagement(unittest.TestCase):
    """LB-038: '물 채움' 버튼으로 물걸레 청소 횟수 초기화 및 물 관리 기능 검증."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.storage = Storage(self.db_path)
        self.config = Config(host="127.0.0.1", port=0, shared_secret="secret-key-1234", db_path=self.db_path)
        self.server = MiniPCServer(self.config, self.storage)
        self.water_service = WaterService(self.storage)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.temp_dir.cleanup()

    def test_file_line_counts_under_300(self) -> None:
        """모든 관련 파일이 300줄 이하인지 확인 (Rules & Constraints)."""
        checked_files = [
            PROJECT_ROOT / "server/models.py",
            PROJECT_ROOT / "server/server.py",
            PROJECT_ROOT / "server/water/service.py",
            PROJECT_ROOT / "server/water/__init__.py",
            SRC_JAVA / "model/WaterTrackerItem.kt",
            SRC_JAVA / "model/WaterTrackerResult.kt",
            SRC_JAVA / "network/ServerClient.kt",
            SRC_JAVA / "WaterManagementActivity.kt",
            SRC_JAVA / "MainActivity.kt",
            RES_LAYOUT / "activity_water_management.xml",
            RES_LAYOUT / "activity_main.xml",
            MANIFEST_PATH,
            Path(__file__),
        ]
        for p in checked_files:
            self.assertTrue(p.is_file(), f"파일 누락: {p}")
            lines = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 300, f"{p.name} 줄 수 초과 ({lines} > 300)")

    def test_water_service_refill_and_counts(self) -> None:
        """WaterService: 물걸레 청소 횟수 기록 및 물 채움 초기화 로직 검증."""
        # 1. 기본 threshold는 7 (LB-037, A-05)
        tracker = self.water_service.get_tracker()
        self.assertEqual(tracker.mop_clean_count, 0)
        self.assertEqual(tracker.alert_threshold_n, 7)
        self.assertIsNone(tracker.last_refilled_at)

        # 2. 청소 횟수 증가
        self.water_service.record_mop_clean()
        self.water_service.record_mop_clean()
        self.water_service.record_mop_clean()
        tracker2 = self.water_service.get_tracker()
        self.assertEqual(tracker2.mop_clean_count, 3)

        # 3. 물 채움 호출 -> 횟수 0 초기화 및 last_refilled_at 갱신 (REQ-019)
        refilled = self.water_service.refill_water()
        self.assertEqual(refilled.mop_clean_count, 0)
        self.assertIsNotNone(refilled.last_refilled_at)

        # 4. 임계값 변경 및 검증
        updated = self.water_service.set_threshold(10)
        self.assertEqual(updated.alert_threshold_n, 10)
        with self.assertRaises(ValueError):
            self.water_service.set_threshold(0)

    def test_http_api_refill_and_security(self) -> None:
        """POST /api/water-tracker/refill API 인증(REQ-024) 및 동작 검증."""
        # 횟수 설정
        self.storage.save_water_tracker(WaterTracker(id="default", mop_clean_count=5))

        # 1. 미인증 요청 거부 (REQ-024)
        code_unauth, _ = simulate_http_request(self.server, "POST", "/api/water-tracker/refill", secret=None)
        self.assertEqual(code_unauth, 401)

        code_wrong, _ = simulate_http_request(self.server, "POST", "/api/water-tracker/refill", secret="wrong")
        self.assertEqual(code_wrong, 401)

        # 2. 올바른 비밀키로 요청 -> 200 OK, mop_clean_count=0
        code, body = simulate_http_request(self.server, "POST", "/api/water-tracker/refill", secret="secret-key-1234")
        self.assertEqual(code, 200)
        self.assertEqual(body["mop_clean_count"], 0)
        self.assertIsNotNone(body["last_refilled_at"])

        # 3. GET /api/water-tracker 로 확인
        code_get, body_get = simulate_http_request(self.server, "GET", "/api/water-tracker", secret="secret-key-1234")
        self.assertEqual(code_get, 200)
        self.assertEqual(body_get["mop_clean_count"], 0)
        self.assertEqual(body_get["last_refilled_at"], body["last_refilled_at"])

    def test_done_when_flow(self) -> None:
        """Done When: '물 채움' 버튼 클릭 시 청소 횟수 0 초기화 & 마지막 물 채움 일시 갱신 종합 검증."""
        # Step 1: 청소 횟수 5회로 축적된 상태 모사
        self.storage.save_water_tracker(WaterTracker(id="default", mop_clean_count=5, last_refilled_at=None))
        t_before = self.storage.get_water_tracker()
        self.assertEqual(t_before.mop_clean_count, 5)
        self.assertIsNone(t_before.last_refilled_at)

        # Step 2: 클라이언트가 '물 채움' 버튼 클릭하여 refillWater() 호출
        code, body = simulate_http_request(self.server, "POST", "/api/water-tracker/refill", secret="secret-key-1234")
        self.assertEqual(code, 200)
        self.assertEqual(body["mop_clean_count"], 0)
        self.assertIsNotNone(body["last_refilled_at"])

        # Step 3: 안드로이드 WaterTrackerItem 모델 파싱 및 표시 검증
        item_source = (SRC_JAVA / "model/WaterTrackerItem.kt").read_text(encoding="utf-8")
        self.assertIn("mopCleanCount", item_source)
        self.assertIn("lastRefilledAt", item_source)
        self.assertIn("formatLastRefilledDisplay", item_source)

        # Step 4: 안드로이드 UI 요소 및 바인딩 계약 검증
        activity_kt = (SRC_JAVA / "WaterManagementActivity.kt").read_text(encoding="utf-8")
        self.assertIn("btnRefillWater", activity_kt)
        self.assertIn("textMopCleanCount", activity_kt)
        self.assertIn("textLastRefilledAt", activity_kt)
        self.assertIn("refillWater", activity_kt)
        self.assertIn("0", body["mop_clean_count"].__str__())

        # 레이아웃 리소스 ID 확인
        layout_xml = (RES_LAYOUT / "activity_water_management.xml").read_text(encoding="utf-8")
        self.assertIn('android:id="@+id/btnRefillWater"', layout_xml)
        self.assertIn('android:id="@+id/textMopCleanCount"', layout_xml)
        self.assertIn('android:id="@+id/textLastRefilledAt"', layout_xml)
        self.assertIn('android:id="@+id/textAlertThreshold"', layout_xml)

        # 매니페스트 등록 확인
        manifest_xml = MANIFEST_PATH.read_text(encoding="utf-8")
        self.assertIn("WaterManagementActivity", manifest_xml)

        # 메인 액티비티 이동 버튼 확인
        main_activity_kt = (SRC_JAVA / "MainActivity.kt").read_text(encoding="utf-8")
        self.assertIn("WaterManagementActivity", main_activity_kt)
        self.assertIn("btnWaterManagement", main_activity_kt)

    def test_conditional_requirement_documented(self) -> None:
        """Must: REQ-018 불가능 시 조건부 요구(REQ-019, D-06, D-14) 적용 확인."""
        caps_doc = (PROJECT_ROOT / "docs/device-capabilities.md").read_text(encoding="utf-8")
        self.assertIn("횟수 방식 필요", caps_doc)
        self.assertIn("REQ-019", caps_doc)
        self.assertTrue("N = 7" in caps_doc or "7회" in caps_doc)
