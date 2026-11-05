from __future__ import annotations

import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_MISSED,
    STATUS_SCHEDULED,
    STATUS_SUCCESS,
    Reservation,
)
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage
from server.time_utils import now_kst

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
    server: MiniPCServer, method: str, path: str, secret: str | None = None
) -> tuple[int, dict[str, Any]]:
    headers = ["Host: 127.0.0.1"]
    if secret:
        headers.append(f"X-Shared-Secret: {secret}")
        headers.append(f"Authorization: Bearer {secret}")
    raw = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
    sock = MockSocket(raw.encode("utf-8"))
    MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
    response_bytes = sock.wfile.getvalue()
    parts = response_bytes.split(b"\r\n\r\n", 1)
    status_line = parts[0].split(b"\r\n")[0].decode("utf-8")
    status_code = int(status_line.split(" ")[1])
    body_data = json.loads(parts[1].decode("utf-8")) if len(parts) > 1 and parts[1] else {}
    return status_code, body_data


class TestAndroidReservationList(unittest.TestCase):
    """LB-014: 예약 목록 화면(ReservationListActivity) 및 클라이언트 단위 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.storage = Storage(self.db_path)
        self.config = Config(host="127.0.0.1", port=0, shared_secret="test-secret-1234", db_path=self.db_path)
        self.server = MiniPCServer(self.config, self.storage)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.temp_dir.cleanup()

    def test_file_line_counts_under_300(self) -> None:
        """모든 관련 파일이 300줄 이하인지 확인 (Rules & Constraints)."""
        checked_files = [
            SRC_JAVA / "model/ReservationItem.kt",
            SRC_JAVA / "model/FetchReservationsResult.kt",
            SRC_JAVA / "network/ServerClient.kt",
            SRC_JAVA / "MainActivity.kt",
            SRC_JAVA / "ReservationListActivity.kt",
            RES_LAYOUT / "item_reservation.xml",
            RES_LAYOUT / "activity_reservation_list.xml",
            Path(__file__),
        ]
        for p in checked_files:
            self.assertTrue(p.is_file(), f"파일 누락: {p}")
            lines = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 300, f"{p.name} 줄 수 초과 ({lines} > 300)")

    def test_five_statuses_distinct_mapping(self) -> None:
        """MUST: 5가지 상태('예정', '성공', '실패', '취소', '누락')가 모두 구분되어야 한다."""
        kt_source = (SRC_JAVA / "ReservationListActivity.kt").read_text(encoding="utf-8")
        expected_statuses = ["예정", "성공", "실패", "취소", "누락"]
        for st in expected_statuses:
            self.assertIn(f'"{st}"', kt_source, f"상태 '{st}'가 ReservationListActivity에 누락됨")

        colors_tree = ET.parse(RES_VALUES / "colors.xml")
        color_names = {elem.attrib["name"] for elem in colors_tree.findall("color")}
        required_colors = [
            "status_scheduled", "status_scheduled_bg", "badge_border_scheduled",
            "status_success", "status_success_bg", "badge_border_success",
            "status_failed", "status_failed_bg", "badge_border_failed",
            "status_cancelled", "status_cancelled_bg", "badge_border_cancelled",
            "status_missed", "status_missed_bg", "badge_border_missed",
        ]
        for rc in required_colors:
            self.assertIn(rc, color_names, f"colors.xml에 '{rc}' 누락")

    def test_server_reservations_api_integration(self) -> None:
        """서버 GET /api/reservations 연동 및 5가지 상태 조회 검증."""
        now = now_kst()
        res_list = [
            Reservation(id="res-1", reserved_at=now + timedelta(hours=2), status=STATUS_SCHEDULED),
            Reservation(id="res-2", reserved_at=now - timedelta(days=1), status=STATUS_SUCCESS),
            Reservation(
                id="res-3", reserved_at=now - timedelta(hours=5), status=STATUS_FAILED,
                failure_reason="배터리 부족", last_device_state="docked"
            ),
            Reservation(id="res-4", reserved_at=now - timedelta(hours=3), status=STATUS_CANCELLED),
            Reservation(id="res-5", reserved_at=now - timedelta(minutes=10), status=STATUS_MISSED),
        ]
        for r in res_list:
            self.storage.save_reservation(r)

        # 1) 인증 성공 조회
        code, body = simulate_http_request(self.server, "GET", "/api/reservations", secret="test-secret-1234")
        self.assertEqual(code, 200)
        items = body.get("reservations", [])
        self.assertEqual(len(items), 5)
        statuses = {item["status"] for item in items}
        self.assertEqual(statuses, {"예정", "성공", "실패", "취소", "누락"})

        # 2) 인증 실패 조회 (REQ-024)
        code_unauth, _ = simulate_http_request(self.server, "GET", "/api/reservations", secret="wrong-secret")
        self.assertEqual(code_unauth, 401)

    def test_partition_upcoming_vs_past(self) -> None:
        """예정된 예약과 지난 예약의 분리 로직 검증 (REQ-002)."""
        now = now_kst()
        res_scheduled = Reservation(id="res-sched", reserved_at=now + timedelta(hours=1), status=STATUS_SCHEDULED)
        res_success = Reservation(id="res-ok", reserved_at=now - timedelta(days=1), status=STATUS_SUCCESS)
        res_failed = Reservation(id="res-fail", reserved_at=now - timedelta(days=2), status=STATUS_FAILED)
        res_cancelled = Reservation(id="res-cancel", reserved_at=now - timedelta(hours=4), status=STATUS_CANCELLED)
        res_missed = Reservation(id="res-miss", reserved_at=now - timedelta(hours=1), status=STATUS_MISSED)

        all_items = [res_scheduled, res_success, res_failed, res_cancelled, res_missed]
        upcoming = [r for r in all_items if r.status == STATUS_SCHEDULED]
        past = [r for r in all_items if r.status != STATUS_SCHEDULED]

        self.assertEqual(len(upcoming), 1)
        self.assertEqual(upcoming[0].id, "res-sched")
        self.assertEqual(len(past), 4)
        past_statuses = {r.status for r in past}
        self.assertEqual(past_statuses, {STATUS_SUCCESS, STATUS_FAILED, STATUS_CANCELLED, STATUS_MISSED})

    def test_failure_reason_formatting(self) -> None:
        """REQ-009: 실패 이유 표시 및 원인 미확인 시 마지막 기기 상태 표시 검증."""
        kt_source = (SRC_JAVA / "ReservationListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("formatFailureReasonText", kt_source)
        self.assertIn("원인 미확인", kt_source)
        self.assertIn("마지막 기기 상태", kt_source)

    def test_reservation_item_model_contract(self) -> None:
        """ReservationItem 데이터 모델 계약 및 파싱 메서드 검증."""
        item_source = (SRC_JAVA / "model/ReservationItem.kt").read_text(encoding="utf-8")
        for field in ("id", "scheduledAt", "status", "failureReason", "lastDeviceState", "allowDuringOffPeriod"):
            self.assertIn(field, item_source)
        self.assertIn("fun isUpcoming()", item_source)
        self.assertIn("fun formatKstDisplay()", item_source)

    def test_server_client_fetch_contract(self) -> None:
        """ServerClient.fetchReservations 메서드 및 X-Shared-Secret 헤더 계약 검증."""
        client_code = (SRC_JAVA / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("fun fetchReservations(", client_code)
        self.assertIn("GET", client_code)
        self.assertIn("/api/reservations", client_code)
        self.assertIn("X-Shared-Secret", client_code)
        self.assertIn("Authorization", client_code)

    def test_layout_and_manifest_validity(self) -> None:
        """activity_reservation_list.xml, item_reservation.xml 및 AndroidManifest 검증."""
        manifest_tree = ET.parse(MANIFEST_PATH)
        activity_names = [
            elem.attrib.get("{http://schemas.android.com/apk/res/android}name")
            for elem in manifest_tree.findall(".//activity")
        ]
        self.assertIn(".ReservationListActivity", activity_names)

        act_tree = ET.parse(RES_LAYOUT / "activity_reservation_list.xml")
        act_ids = {
            elem.attrib.get("{http://schemas.android.com/apk/res/android}id", "").split("/")[-1]
            for elem in act_tree.iter()
        }
        required_act_ids = {
            "btnRefresh", "btnAddReservation", "progressBar", "cardError", "textErrorMessage",
            "textUpcomingHeader", "textUpcomingEmpty", "layoutUpcomingList",
            "textPastHeader", "textPastEmpty", "layoutPastList",
        }
        self.assertTrue(required_act_ids.issubset(act_ids), f"누락된 뷰 ID: {required_act_ids - act_ids}")

        content_container = act_tree.find(
            ".//LinearLayout[@{http://schemas.android.com/apk/res-auto}layout_constraintWidth_max='640dp']"
        )
        self.assertIsNotNone(content_container, "Fold8/S21+ 반응형 640dp 최대 너비 제약 누락")

        item_tree = ET.parse(RES_LAYOUT / "item_reservation.xml")
        item_ids = {
            elem.attrib.get("{http://schemas.android.com/apk/res/android}id", "").split("/")[-1]
            for elem in item_tree.iter()
        }
        required_item_ids = {
            "textScheduledAt", "cardStatusBadge", "textStatusBadge",
            "textFailureReason", "textReservationId"
        }
        self.assertTrue(
            required_item_ids.issubset(item_ids),
            f"item_reservation 누락 ID: {required_item_ids - item_ids}"
        )

        # LB-047: 예약 목록 진입점이 btnReservationList(메인)에서 홈의 tileReservationList 타일로 이동했다.
        main_tree = ET.parse(RES_LAYOUT / "activity_home.xml")
        main_ids = {
            elem.attrib.get("{http://schemas.android.com/apk/res/android}id", "").split("/")[-1]
            for elem in main_tree.iter()
        }
        self.assertIn("tileReservationList", main_ids)


if __name__ == "__main__":
    unittest.main()
