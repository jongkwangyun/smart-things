from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta

from server.models import (
    STATUS_FAILED,
    STATUS_SCHEDULED,
    STATUS_SUCCESS,
    AutoStopEvent,
    NotificationTarget,
    OffPeriod,
    Reservation,
)
from server.storage import Storage
from server.time_utils import KST, now_kst


class TestStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = f"{self.temp_dir.name}/test.db"
        self.storage = Storage(self.db_path)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_reservation_crud(self) -> None:
        res_time = now_kst() + timedelta(hours=2)
        res = Reservation(
            id="res-001",
            reserved_at=res_time,
            status=STATUS_SCHEDULED,
            allow_during_off_period=True,
        )
        self.storage.save_reservation(res)

        fetched = self.storage.get_reservation("res-001")
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.id, "res-001")
        self.assertEqual(fetched.status, STATUS_SCHEDULED)
        self.assertTrue(fetched.allow_during_off_period)
        self.assertIsNone(fetched.failure_reason)

        # Update reservation
        fetched.status = STATUS_FAILED
        fetched.failure_reason = "배터리 부족"
        fetched.last_device_state = "docked_charging"
        self.storage.save_reservation(fetched)

        updated = self.storage.get_reservation("res-001")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.status, STATUS_FAILED)
        self.assertEqual(updated.failure_reason, "배터리 부족")
        self.assertEqual(updated.last_device_state, "docked_charging")

        # List
        all_res = self.storage.list_reservations()
        self.assertEqual(len(all_res), 1)

        # Delete
        self.assertTrue(self.storage.delete_reservation("res-001"))
        self.assertIsNone(self.storage.get_reservation("res-001"))

    def test_off_period_crud(self) -> None:
        start = datetime(2026, 12, 1, 0, 0, 0, tzinfo=KST)
        end = datetime(2026, 12, 5, 23, 59, 59, tzinfo=KST)
        op = OffPeriod(id="op-001", start_at=start, end_at=end)
        self.storage.save_off_period(op)

        fetched = self.storage.get_off_period("op-001")
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.start_at, start)
        self.assertEqual(fetched.end_at, end)

        periods = self.storage.list_off_periods()
        self.assertEqual(len(periods), 1)

        self.assertTrue(self.storage.delete_off_period("op-001"))
        self.assertIsNone(self.storage.get_off_period("op-001"))

    def test_auto_stop_event(self) -> None:
        start = datetime(2026, 12, 1, 0, 0, 0, tzinfo=KST)
        end = datetime(2026, 12, 5, 23, 59, 59, tzinfo=KST)
        op = OffPeriod(id="op-002", start_at=start, end_at=end)
        self.storage.save_off_period(op)

        event = AutoStopEvent(
            id="evt-001",
            occurred_at=start + timedelta(hours=10),
            off_period_id=op.id,
            success=True,
            details="스마트싱스 반복 예약 시작 감지 후 정지 성공",
        )
        self.storage.save_auto_stop_event(event)

        events = self.storage.list_auto_stop_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].off_period_id, "op-002")
        self.assertTrue(events[0].success)

    def test_water_tracker(self) -> None:
        tracker = self.storage.get_water_tracker()
        self.assertEqual(tracker.mop_clean_count, 0)
        self.assertEqual(tracker.alert_threshold_n, 7)

        refill_time = now_kst()
        tracker.last_refilled_at = refill_time
        tracker.mop_clean_count = 3
        tracker.alert_threshold_n = 10
        self.storage.save_water_tracker(tracker)

        updated = self.storage.get_water_tracker()
        self.assertEqual(updated.mop_clean_count, 3)
        self.assertEqual(updated.alert_threshold_n, 10)
        self.assertIsNotNone(updated.last_refilled_at)

    def test_notification_target_two_devices(self) -> None:
        # 사용자 2명 (AC-1)
        target1 = NotificationTarget(
            id="nt-1",
            device_id="galaxy-s21-plus",
            push_token="token-s21-plus-sample",
        )
        target2 = NotificationTarget(
            id="nt-2",
            device_id="galaxy-fold-8",
            push_token="token-fold-8-sample",
        )
        self.storage.save_notification_target(target1)
        self.storage.save_notification_target(target2)

        targets = self.storage.list_notification_targets()
        self.assertEqual(len(targets), 2)

        # Update existing token
        target1.push_token = "updated-token"
        self.storage.save_notification_target(target1)
        fetched = self.storage.get_notification_target("galaxy-s21-plus")
        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.push_token, "updated-token")

    def test_persistence_across_storage_reopen(self) -> None:
        # 저장 후 닫기
        res = Reservation(
            id="res-persist",
            reserved_at=now_kst() + timedelta(days=1),
            status=STATUS_SUCCESS,
        )
        op = OffPeriod(
            id="op-persist",
            start_at=now_kst(),
            end_at=now_kst() + timedelta(days=3),
        )
        self.storage.save_reservation(res)
        self.storage.save_off_period(op)
        self.storage.close()

        # 새로운 Storage 인스턴스로 다시 열기 (재부팅/재시작 시뮬레이션)
        reopened = Storage(self.db_path)
        try:
            persisted_res = reopened.get_reservation("res-persist")
            self.assertIsNotNone(persisted_res)
            assert persisted_res is not None
            self.assertEqual(persisted_res.status, STATUS_SUCCESS)

            persisted_op = reopened.get_off_period("op-persist")
            self.assertIsNotNone(persisted_op)
            assert persisted_op is not None
            self.assertEqual(persisted_op.id, "op-persist")
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
