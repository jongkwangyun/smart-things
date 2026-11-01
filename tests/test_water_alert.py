from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.models import STATUS_SCHEDULED, STATUS_SUCCESS, Reservation
from server.notify import FCMClient, NotificationService
from server.scheduler import ReservationScheduler
from server.smartthings.client import DeviceStatus
from server.storage import Storage
from server.time_utils import KST
from server.water.service import WaterService

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MockSmartThingsClient:
    """테스트용 스마트싱스 클라이언트."""

    def __init__(self, operating_state: str = "cleaning") -> None:
        self.operating_state = operating_state

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        return {"results": [{"id": f"cmd-{device_id}", "status": "COMPLETED"}]}

    def get_device_status(self, device_id: str) -> DeviceStatus:
        return DeviceStatus(
            device_id=device_id,
            operating_state=self.operating_state,
            raw_status={
                "components": {
                    "main": {
                        "robotCleanerOperatingState": {"operatingState": {"value": self.operating_state}},
                        "robotCleanerMovement": {"movement": {"value": "cleaning"}},
                    }
                }
            },
        )


class TestWaterAlert(unittest.TestCase):
    """LB-039: 물걸레 청소 횟수 N 도달 시 물 부족 알림 검증 (REQ-020, D-06, D-12, D-14)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_water_alert.db"
        self.storage = Storage(self.db_path)
        self.fcm_client = FCMClient(simulated=True)
        self.notify_service = NotificationService(self.storage, self.fcm_client)
        self.notify_service.register_device("galaxy-s21-plus", "token-s21", target_id="target-s21")
        self.notify_service.register_device("galaxy-fold8", "token-fold8", target_id="target-fold8")
        self.water_service = WaterService(self.storage, notification_service=self.notify_service)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_count_reaches_n_alerts_both_devices(self) -> None:
        """물걸레 청소가 N회에 도달하면 두 기기(S21+, Fold8)에 알림 전송 (Done When, REQ-020, D-12)."""
        self.water_service.set_threshold(3)

        # 1회, 2회: N 미만이므로 알림 미발송
        _, alerted1 = self.water_service.record_mop_clean(is_mop=True)
        self.assertFalse(alerted1)
        _, alerted2 = self.water_service.record_mop_clean(is_mop=True)
        self.assertFalse(alerted2)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 0)

        # 3회: N(=3) 도달 -> 두 휴대폰에 물 부족 알림 발송
        tracker, alerted3 = self.water_service.record_mop_clean(is_mop=True)
        self.assertTrue(alerted3)
        self.assertEqual(tracker.mop_clean_count, 3)
        self.assertIsNotNone(tracker.last_alerted_at)

        # 두 기기 발송 확인
        self.assertEqual(len(self.fcm_client.dispatched_messages), 2)
        received_devices = {msg["device_id"] for msg in self.fcm_client.dispatched_messages}
        self.assertEqual(received_devices, {"galaxy-s21-plus", "galaxy-fold8"})

        for msg in self.fcm_client.dispatched_messages:
            self.assertEqual(msg["title"], "[로봇청소기] 물 부족 알림")
            self.assertIn("3회(기준: 3회)에 도달했습니다. 물을 채워주세요.", msg["body"])

    def test_count_n_plus_1_same_cycle_no_duplicate_alert(self) -> None:
        """같은 주기에 알림을 반복해서 보내지 않는다 (Must rule)."""
        self.water_service.set_threshold(2)

        self.water_service.record_mop_clean(is_mop=True)
        _, alerted2 = self.water_service.record_mop_clean(is_mop=True)
        self.assertTrue(alerted2)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 2)

        # N+1회 수행: 횟수는 3으로 증가하되 중복 알림은 발송되지 않음
        tracker, alerted3 = self.water_service.record_mop_clean(is_mop=True)
        self.assertFalse(alerted3)
        self.assertEqual(tracker.mop_clean_count, 3)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 2)

    def test_non_mop_cleaning_does_not_increment_count(self) -> None:
        """물걸레 청소만 센다 (is_mop=False는 횟수 미증가, Must rule)."""
        self.water_service.set_threshold(5)
        self.water_service.record_mop_clean(is_mop=True)
        self.water_service.record_mop_clean(is_mop=True)

        initial = self.water_service.get_tracker()
        self.assertEqual(initial.mop_clean_count, 2)

        # 일반(흡입 전용) 청소 수행 시 물걸레 카운트 미증가
        tracker, alerted = self.water_service.record_mop_clean(is_mop=False)
        self.assertFalse(alerted)
        self.assertEqual(tracker.mop_clean_count, 2)
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 2)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 0)

    def test_refill_resets_cycle_and_allows_new_alert(self) -> None:
        """물 채움 시 카운트가 0으로 초기화되고 새 주기에서 N 도달 시 다시 알림 (REQ-019, REQ-020)."""
        self.water_service.set_threshold(2)
        self.water_service.record_mop_clean(is_mop=True)
        self.water_service.record_mop_clean(is_mop=True)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 2)

        # 물 채움: 카운트 0 초기화 및 새 주기 시작
        refilled = self.water_service.refill_water()
        self.assertEqual(refilled.mop_clean_count, 0)
        self.assertIsNotNone(refilled.last_refilled_at)

        # 새 주기 1회차: N 미만이므로 알림 미발송
        _, alerted_new1 = self.water_service.record_mop_clean(is_mop=True)
        self.assertFalse(alerted_new1)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 2)

        # 새 주기 2회차: 새 주기의 N 도달로 새 알림 발송
        tracker_new2, alerted_new2 = self.water_service.record_mop_clean(is_mop=True)
        self.assertTrue(alerted_new2)
        self.assertEqual(tracker_new2.mop_clean_count, 2)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 4)

    def test_scheduler_integration_increments_and_alerts_at_n(self) -> None:
        """스케줄러 예약 실행 성공 시 물걸레 카운트 증가 및 N 도달 시 알림 연동 (Done When)."""
        client = MockSmartThingsClient(operating_state="cleaning")
        scheduler = ReservationScheduler(
            storage=self.storage,
            client=client,  # type: ignore[arg-type]
            notification_service=self.notify_service,
            water_service=self.water_service,
        )
        self.water_service.set_threshold(2)
        now = datetime(2026, 11, 1, 10, 0, 0, tzinfo=KST)
        scheduler.now_func = lambda: now

        # 1차 예약 성공 -> 물걸레 카운트 1
        res1 = self.storage.save_reservation(
            Reservation(id="res-1", reserved_at=now - timedelta(seconds=10), status=STATUS_SCHEDULED)
        )
        scheduler.step()
        r1 = self.storage.get_reservation(res1.id)
        assert r1 is not None
        self.assertEqual(r1.status, STATUS_SUCCESS)
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 1)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 0)

        # 2차 예약 성공 -> 물걸레 카운트 2(=N) 도달 -> 알림 발송!
        res2 = self.storage.save_reservation(
            Reservation(id="res-2", reserved_at=now - timedelta(seconds=5), status=STATUS_SCHEDULED)
        )
        scheduler.step()
        r2 = self.storage.get_reservation(res2.id)
        assert r2 is not None
        self.assertEqual(r2.status, STATUS_SUCCESS)
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 2)
        self.assertEqual(len(self.fcm_client.dispatched_messages), 2)

    def test_file_line_counts(self) -> None:
        """모든 관련 파일이 줄 수 제한(300줄 이하)을 준수하는지 확인."""
        files = [
            PROJECT_ROOT / "server/water/service.py",
            PROJECT_ROOT / "server/scheduler/scheduler.py",
            Path(__file__),
        ]
        for f in files:
            lines = len(f.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 300, f"{f.name} 줄 수 초과 ({lines} > 300)")
        self.assertLessEqual(
            len(Path(__file__).read_text(encoding="utf-8").splitlines()),
            250,
            "test_water_alert.py는 250줄 이하여야 합니다.",
        )


if __name__ == "__main__":
    unittest.main()
