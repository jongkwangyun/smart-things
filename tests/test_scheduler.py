from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import (
    STATUS_CANCELLED,
    STATUS_MISSED,
    STATUS_SCHEDULED,
    STATUS_SUCCESS,
    Reservation,
)
from server.scheduler import (
    KSTFormatter,
    ReservationScheduler,
    get_scheduler_logger,
)
from server.server import MiniPCServer
from server.smartthings.client import SmartThingsApiError
from server.storage import Storage
from server.time_utils import KST


class DummyClient:
    """테스트용 가짜 스마트싱스 클라이언트."""

    def __init__(self) -> None:
        self.started_devices: list[str] = []
        self.should_raise: Exception | None = None

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        if self.should_raise:
            raise self.should_raise
        self.started_devices.append(device_id)
        return {"results": [{"id": f"cmd-{device_id}", "status": "COMPLETED"}]}


class TestScheduler(unittest.TestCase):
    def setUp(self) -> None:
        self.fixed_now = datetime(2026, 10, 31, 14, 0, 0, tzinfo=KST)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "sched_test.db"
        self.log_path = Path(self.temp_dir.name) / "scheduler_test.log"
        self.storage = Storage(self.db_path)
        self.dummy_client = DummyClient()
        self.scheduler = ReservationScheduler(
            storage=self.storage,
            client=self.dummy_client,  # type: ignore[arg-type]
            device_id="test-cleaner-device",
            poll_interval=0.05,
            now_func=lambda: self.fixed_now,
            log_file=self.log_path,
        )

    def tearDown(self) -> None:
        if self.scheduler.is_running:
            self.scheduler.stop(timeout=1.0)
        self.storage.close()
        self.temp_dir.cleanup()

    def test_kst_formatter_and_logger(self) -> None:
        logger = get_scheduler_logger(self.log_path)
        logger.info("테스트 로그 메시지")
        for h in logger.handlers:
            h.flush()

        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("테스트 로그 메시지", content)

        # KSTFormatter 단위 테스트
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="formatter test", args=(), exc_info=None,
        )
        record.created = self.fixed_now.timestamp()
        formatter = KSTFormatter(datefmt="%Y-%m-%d %H:%M:%S")
        formatted_time = formatter.formatTime(record, datefmt="%Y-%m-%d %H:%M:%S")
        self.assertEqual(formatted_time, "2026-10-31 14:00:00")

    def test_scheduler_init_defaults(self) -> None:
        sched = ReservationScheduler(storage=self.storage, log_file=self.log_path)
        self.assertIsNotNone(sched.client)
        self.assertTrue(sched.device_id)
        self.assertEqual(sched.poll_interval, 1.0)
        self.assertFalse(sched.is_running)

    def test_step_executes_eligible_reservation(self) -> None:
        res_time = self.fixed_now - timedelta(minutes=2)
        res = Reservation(
            id="res-001",
            reserved_at=res_time,
            status=STATUS_SCHEDULED,
        )
        self.storage.save_reservation(res)

        executed = self.scheduler.step()
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0].id, "res-001")
        self.assertEqual(self.dummy_client.started_devices, ["test-cleaner-device"])

        # 영구 저장소에 executed_at이 기록되었는지 확인 (LB-016)
        updated = self.storage.get_reservation("res-001")
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.executed_at, self.fixed_now)

        # 로그 파일에 명령 기록 남았는지 확인 (LB-015 done_when)
        log_content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("res-001", log_content)
        self.assertIn("test-cleaner-device", log_content)
        self.assertIn("COMPLETED", log_content)

    def test_step_does_not_execute_future_reservation(self) -> None:
        future_time = self.fixed_now + timedelta(minutes=3)
        res = Reservation(id="res-future", reserved_at=future_time, status=STATUS_SCHEDULED)
        self.storage.save_reservation(res)

        executed = self.scheduler.step()
        self.assertEqual(len(executed), 0)
        self.assertEqual(len(self.dummy_client.started_devices), 0)

        persisted = self.storage.get_reservation("res-future")
        assert persisted is not None
        self.assertIsNone(persisted.executed_at)

    def test_step_does_not_double_execute(self) -> None:
        res = Reservation(
            id="res-done",
            reserved_at=self.fixed_now - timedelta(minutes=1),
            status=STATUS_SCHEDULED,
            executed_at=self.fixed_now - timedelta(minutes=1),
        )
        self.storage.save_reservation(res)

        executed = self.scheduler.step()
        self.assertEqual(len(executed), 0)
        self.assertEqual(len(self.dummy_client.started_devices), 0)

    def test_step_does_not_execute_cancelled_or_success(self) -> None:
        r_cancel = Reservation(id="r-c", reserved_at=self.fixed_now - timedelta(minutes=1), status=STATUS_CANCELLED)
        r_succ = Reservation(id="r-s", reserved_at=self.fixed_now - timedelta(minutes=1), status=STATUS_SUCCESS)
        self.storage.save_reservation(r_cancel)
        self.storage.save_reservation(r_succ)

        executed = self.scheduler.step()
        self.assertEqual(len(executed), 0)
        self.assertEqual(len(self.dummy_client.started_devices), 0)

    def test_step_marks_missed_when_older_than_5_minutes(self) -> None:
        old_time = self.fixed_now - timedelta(minutes=6)
        res = Reservation(id="res-missed", reserved_at=old_time, status=STATUS_SCHEDULED)
        self.storage.save_reservation(res)

        executed = self.scheduler.step()
        self.assertEqual(len(executed), 0)
        self.assertEqual(len(self.dummy_client.started_devices), 0)

        updated = self.storage.get_reservation("res-missed")
        assert updated is not None
        self.assertEqual(updated.status, STATUS_MISSED)
        self.assertIn("누락", str(updated.failure_reason))

    def test_step_handles_client_exception_gracefully(self) -> None:
        self.dummy_client.should_raise = SmartThingsApiError("스마트싱스 통신 장애")
        res = Reservation(id="res-err", reserved_at=self.fixed_now - timedelta(seconds=10), status=STATUS_SCHEDULED)
        self.storage.save_reservation(res)

        executed = self.scheduler.step()
        self.assertEqual(len(executed), 1)

        updated = self.storage.get_reservation("res-err")
        assert updated is not None
        self.assertEqual(updated.executed_at, self.fixed_now)
        self.assertIn("명령 전송 실패", str(updated.failure_reason))

        log_content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("명령 전송 실패", log_content)

    def test_scheduler_thread_lifecycle(self) -> None:
        self.assertFalse(self.scheduler.is_running)
        self.scheduler.start()
        self.assertTrue(self.scheduler.is_running)

        # 중복 start() 호출 시 안전성 확인
        self.scheduler.start()
        self.assertTrue(self.scheduler.is_running)

        self.scheduler.stop(timeout=1.0)
        self.assertFalse(self.scheduler.is_running)

    def test_server_close_stops_scheduler(self) -> None:
        cfg = Config(host="127.0.0.1", port=0, db_path=self.db_path)
        srv = MiniPCServer(config=cfg, storage=self.storage, scheduler=self.scheduler)
        self.scheduler.start()
        self.assertTrue(self.scheduler.is_running)

        srv.close()
        self.assertFalse(self.scheduler.is_running)
