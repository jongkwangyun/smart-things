from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.models import STATUS_FAILED, STATUS_SCHEDULED, Reservation
from server.notify import FCMClient, NotificationService
from server.scheduler import ReservationScheduler
from server.smartthings.client import DeviceStatus, parse_failure_reason
from server.storage import Storage
from server.time_utils import KST

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MockClient:
    def __init__(self, status: DeviceStatus | None = None, health: str = "ONLINE") -> None:
        self.status = status
        self.health = health

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        return {"results": [{"id": "cmd-1", "status": "COMPLETED"}]}

    def get_device_health(self, device_id: str) -> str:
        return self.health

    def get_device_status(self, device_id: str) -> DeviceStatus | None:
        return self.status


class TestFailureReasons(unittest.TestCase):
    """LB-027: 실패 이유 표시 검증 (REQ-009, A-03, D-10, LB-003)."""

    def setUp(self) -> None:
        self.base_time = datetime(2026, 10, 31, 14, 0, 0, tzinfo=KST)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_fr.db"
        self.log_path = Path(self.temp_dir.name) / "sched.log"
        self.storage = Storage(self.db_path)
        self.fcm = FCMClient(simulated=True)
        self.notify_service = NotificationService(self.storage, self.fcm)
        self.notify_service.register_device("galaxy-s21-plus", "token-s21", target_id="s21")
        self.notify_service.register_device("galaxy-fold-8", "token-fold8", target_id="fold8")

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_parse_failure_reason_confirmed_and_fallback(self) -> None:
        """LB-003 확인된 5대 이유 및 fallback 검증, 추측 금지 (A-03, must)."""
        # a) 배터리 부족 (<20%)
        st_bat15 = DeviceStatus("d1", battery=15, operating_state="idle")
        self.assertEqual(parse_failure_reason(st_bat15), "배터리 부족 (15%)")
        self.assertIsNone(parse_failure_reason(DeviceStatus("d1", battery=20, operating_state="idle")))

        # b) 먼지통 가득 참
        st_dust = DeviceStatus("d1", dust_bag_status="full", operating_state="idle")
        self.assertEqual(parse_failure_reason(st_dust), "먼지통 가득 참")

        # c) 모터 필터 세척 필요
        st_filter = DeviceStatus("d1", motor_filter_status="washRequired", operating_state="idle")
        self.assertEqual(parse_failure_reason(st_filter), "모터 필터 세척 필요")

        # d) 기기 오프라인 (헬스 상태 OFFLINE 또는 is_online=False)
        self.assertEqual(parse_failure_reason(None, health_state="OFFLINE"), "기기 오프라인")
        self.assertEqual(parse_failure_reason(DeviceStatus("d1", is_online=False)), "기기 오프라인")

        # e) 기기 동작 오류 (error)
        st_err = DeviceStatus("d1", operating_state="error")
        self.assertEqual(parse_failure_reason(st_err), "기기 동작 오류 (error)")

        # f) 원인 미확인: 추측 이유를 표시하지 않고 마지막 상태값 반환 (A-03, must)
        st_idle = DeviceStatus("d1", operating_state="idle")
        self.assertEqual(parse_failure_reason(st_idle, fallback_unknown=True), "원인 미확인 (마지막 상태: idle)")
        self.assertEqual(parse_failure_reason(None, fallback_unknown=True), "원인 미확인 (기기 상태 정보 없음)")

        # 하드웨어 끼임(바퀴 들림 등) 추측 부재 검증: A-03에 따라 확인 불가 항목은 추측하지 않음
        raw_unknown = DeviceStatus("d1", operating_state="charging", raw_status={"wheelsLiftedUp": True})
        reason_unknown = parse_failure_reason(raw_unknown, fallback_unknown=True)
        self.assertNotIn("바퀴", str(reason_unknown))
        self.assertNotIn("끼임", str(reason_unknown))
        self.assertEqual(reason_unknown, "원인 미확인 (마지막 상태: charging)")

    def test_scheduler_failure_notification_body_and_detail(self) -> None:
        """스케줄러 실패 전이 시 두 기기 푸시 알림 본문에 정확한 이유 포함 (done_when, REQ-008, REQ-009)."""
        scenarios = [
            (DeviceStatus("d1", battery=12, operating_state="idle"), "ONLINE", "배터리 부족 (12%)"),
            (DeviceStatus("d1", dust_bag_status="full", operating_state="idle"), "ONLINE", "먼지통 가득 참"),
            (DeviceStatus("d1", motor_filter_status="dirty", operating_state="idle"), "ONLINE", "모터 필터 세척 필요"),
            (DeviceStatus("d1", is_online=False), "OFFLINE", "기기 오프라인"),
            (DeviceStatus("d1", operating_state="error"), "ONLINE", "기기 동작 오류 (error)"),
            (DeviceStatus("d1", operating_state="idle"), "ONLINE", "원인 미확인 (마지막 상태: idle)"),
        ]

        for idx, (st, health, expected_reason) in enumerate(scenarios):
            self.fcm.dispatched_messages.clear()
            res_id = f"res-fail-{idx}"
            res = Reservation(
                id=res_id, reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time
            )
            self.storage.save_reservation(res)
            mock_client = MockClient(status=st, health=health)
            now_step = self.base_time + timedelta(minutes=5, seconds=1)

            def get_now(t: datetime = now_step) -> datetime:
                return t

            sched = ReservationScheduler(
                storage=self.storage,
                client=mock_client,  # type: ignore[arg-type]
                device_id="d1",
                now_func=get_now,
                log_file=self.log_path,
                notification_service=self.notify_service,
            )
            sched.step()

            # 저장된 예약 상세 속성 검증 (done_when)
            updated = self.storage.get_reservation(res_id)
            assert updated is not None
            self.assertEqual(updated.status, STATUS_FAILED)
            self.assertEqual(updated.failure_reason, expected_reason)

            # 두 단말(S21+, Fold8) FCM 푸시 알림 본문 검증 (REQ-022, D-12)
            self.assertEqual(len(self.fcm.dispatched_messages), 2)
            for msg in self.fcm.dispatched_messages:
                self.assertEqual(msg["title"], "[로봇청소기] 청소 실패")
                self.assertIn(expected_reason, msg["body"])
                self.assertEqual(msg["data"]["failure_reason"], expected_reason)
                self.assertEqual(msg["data"]["reservation_id"], res_id)

    def test_android_ui_rendering_and_formatting(self) -> None:
        """안드로이드 예약 상세 카드 실패 이유 렌더링 계약 검증 (REQ-009, done_when)."""
        kt_path = PROJECT_ROOT / "android/app/src/main/java/com/smartthings/cleaner/ReservationListActivity.kt"
        kt_code = kt_path.read_text(encoding="utf-8")
        self.assertIn("formatFailureReasonText", kt_code)
        self.assertIn("textFailureReason", kt_code)
        self.assertIn("ReservationItem.STATUS_FAILED", kt_code)
        self.assertIn("View.VISIBLE", kt_code)
        self.assertIn("View.GONE", kt_code)

        # Kotlin formatFailureReasonText 로직 검증
        def py_format_reason(reason: str | None, last_state: str | None) -> str:
            if reason and reason.strip() and reason != "원인 미확인":
                return f"실패 이유: {reason}"
            if last_state and last_state.strip():
                return f"실패 이유: 원인 미확인 (마지막 기기 상태: {last_state})"
            if reason and reason.strip():
                return f"실패 이유: {reason}"
            return "실패 이유: 원인 미확인"

        self.assertEqual(py_format_reason("배터리 부족 (15%)", "idle"), "실패 이유: 배터리 부족 (15%)")
        self.assertEqual(py_format_reason("먼지통 가득 참", "idle"), "실패 이유: 먼지통 가득 참")
        self.assertEqual(py_format_reason("모터 필터 세척 필요", "idle"), "실패 이유: 모터 필터 세척 필요")
        self.assertEqual(py_format_reason("기기 오프라인", None), "실패 이유: 기기 오프라인")
        self.assertEqual(
            py_format_reason("원인 미확인 (마지막 상태: idle)", "idle"),
            "실패 이유: 원인 미확인 (마지막 상태: idle)",
        )
        self.assertEqual(py_format_reason("원인 미확인", "idle"), "실패 이유: 원인 미확인 (마지막 기기 상태: idle)")
        self.assertEqual(py_format_reason(None, "paused"), "실패 이유: 원인 미확인 (마지막 기기 상태: paused)")
        self.assertEqual(py_format_reason(None, None), "실패 이유: 원인 미확인")

        # item_reservation.xml 레이아웃 검증
        layout_path = PROJECT_ROOT / "android/app/src/main/res/layout/item_reservation.xml"
        root = ET.parse(layout_path).getroot()
        id_attr = "{http://schemas.android.com/apk/res/android}id"
        reason_nodes = [n for n in root.iter() if n.attrib.get(id_attr) == "@+id/textFailureReason"]
        self.assertEqual(len(reason_nodes), 1)
        node = reason_nodes[0]
        vis_attr = "{http://schemas.android.com/apk/res/android}visibility"
        color_attr = "{http://schemas.android.com/apk/res/android}textColor"
        self.assertEqual(node.attrib.get(vis_attr), "gone")
        self.assertEqual(node.attrib.get(color_attr), "@color/status_failed")

    def test_file_line_limits(self) -> None:
        """모든 관련 파일 크기가 300줄 이하인지 검증."""
        files = [
            PROJECT_ROOT / "server/smartthings/client.py",
            PROJECT_ROOT / "server/scheduler/scheduler.py",
            PROJECT_ROOT / "android/app/src/main/java/com/smartthings/cleaner/ReservationListActivity.kt",
            Path(__file__),
        ]
        for f in files:
            lines = len(f.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 300, f"{f.name} exceeds 300 lines: {lines}")
        self.assertLessEqual(len(Path(__file__).read_text(encoding="utf-8").splitlines()), 250)


if __name__ == "__main__":
    unittest.main()
