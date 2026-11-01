from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import OffPeriod, Reservation
from server.notify import FCMClient, NotificationService
from server.off_periods.auto_stop import DEFAULT_DEVICE_ID, AutoStopWatcher
from server.scheduler.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer, create_server
from server.smartthings.client import DeviceStatus
from server.storage import Storage
from server.time_utils import KST


class DummySocket:
    """테스트용 인메모리 HTTP 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class MockSmartThingsClient:
    def __init__(self, status: DeviceStatus | None = None) -> None:
        self.status = status or DeviceStatus(
            device_id=DEFAULT_DEVICE_ID,
            operating_state="idle",
            battery=100,
            is_online=True,
        )
        self.return_to_home_calls: list[str] = []

    def get_device_status(self, device_id: str) -> DeviceStatus:
        return self.status

    def return_to_home(self, device_id: str) -> dict[str, Any]:
        self.return_to_home_calls.append(device_id)
        return {"results": [{"id": "mock-return-cmd", "status": "COMPLETED"}]}

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        return {"results": [{"id": "mock-start-cmd", "status": "COMPLETED"}]}


class TestAutoStop(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_autostop.db"
        self.storage = Storage(self.db_path)
        self.fcm_client = FCMClient(simulated=True)
        self.notify_service = NotificationService(self.storage, self.fcm_client)
        self.client = MockSmartThingsClient()
        self.watcher = AutoStopWatcher(
            storage=self.storage,
            client=self.client,  # type: ignore[arg-type]
            device_id=DEFAULT_DEVICE_ID,
            notification_service=self.notify_service,
        )
        self.base_time = datetime(2026, 11, 1, 10, 0, 0, tzinfo=KST)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def _http_get(self, server: MiniPCServer, path: str, secret: str = "test-secret") -> tuple[int, dict[str, Any]]:
        raw = f"GET {path} HTTP/1.1\r\nHost: localhost\r\nX-Shared-Secret: {secret}\r\n\r\n".encode()
        sock = DummySocket(raw)
        MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
        output = sock.wfile.getvalue().decode("utf-8")
        status_line = output.splitlines()[0]
        code = int(status_line.split(" ")[1])
        body_str = output.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in output else ""
        return code, json.loads(body_str) if body_str else {}

    def test_no_active_off_period_does_not_stop(self) -> None:
        self.storage.save_off_period(OffPeriod(
            id="op-past",
            start_at=self.base_time - timedelta(hours=3),
            end_at=self.base_time - timedelta(hours=1),
        ))
        self.client.status = DeviceStatus(
            device_id=DEFAULT_DEVICE_ID, operating_state="cleaning", battery=90, is_online=True
        )
        event = self.watcher.step(self.base_time)
        self.assertIsNone(event)
        self.assertEqual(len(self.client.return_to_home_calls), 0)
        self.assertEqual(len(self.storage.list_auto_stop_events()), 0)

    def test_active_off_period_stops_cleaner_and_records_event(self) -> None:
        op = self.storage.save_off_period(OffPeriod(
            id="op-1",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        ))
        self.notify_service.register_device("s21-plus", "token-s21")
        self.notify_service.register_device("fold-8", "token-fold8")
        self.client.status = DeviceStatus(
            device_id=DEFAULT_DEVICE_ID, operating_state="cleaning", battery=95, is_online=True
        )

        event = self.watcher.step(self.base_time)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.off_period_id, op.id)
        self.assertTrue(event.success)
        self.assertEqual(self.client.return_to_home_calls, [DEFAULT_DEVICE_ID])

        # Must rule: 정지까지 걸린 시간을 기록해 둔다
        self.assertIn("정지 소요 시간:", event.details or "")
        self.assertIn("초", event.details or "")

        # Notifications sent to both users
        self.assertEqual(len(self.fcm_client.dispatched_messages), 2)
        devs = {m["device_id"] for m in self.fcm_client.dispatched_messages}
        self.assertEqual(devs, {"s21-plus", "fold-8"})
        self.assertIn("쉬는 기간 자동 정지", self.fcm_client.dispatched_messages[0]["title"])

        # Done When: HTTP GET /api/auto-stop-events returns event
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret="test-secret"))
        try:
            status, data = self._http_get(srv, "/api/auto-stop-events")
            self.assertEqual(status, 200)
            events = data.get("auto_stop_events", [])
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["id"], event.id)
            self.assertEqual(events[0]["off_period_id"], op.id)
        finally:
            srv.close()

    def test_active_off_period_processing_state_stopped(self) -> None:
        self.storage.save_off_period(OffPeriod(
            id="op-proc",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        ))
        self.client.status = DeviceStatus(
            device_id=DEFAULT_DEVICE_ID, operating_state="processing", battery=80, is_online=True
        )
        event = self.watcher.step(self.base_time)
        self.assertIsNotNone(event)
        self.assertEqual(self.client.return_to_home_calls, [DEFAULT_DEVICE_ID])

    def test_allowed_reservation_during_off_period_not_stopped(self) -> None:
        self.storage.save_off_period(OffPeriod(
            id="op-allowed",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        ))
        self.storage.save_reservation(Reservation(
            id="res-allowed",
            reserved_at=self.base_time - timedelta(minutes=4),
            status="예정",
            allow_during_off_period=True,
            executed_at=self.base_time - timedelta(minutes=4),
        ))
        self.client.status = DeviceStatus(
            device_id=DEFAULT_DEVICE_ID, operating_state="cleaning", battery=90, is_online=True
        )
        event = self.watcher.step(self.base_time)
        self.assertIsNone(event)
        self.assertEqual(len(self.client.return_to_home_calls), 0)

    def test_non_cleaning_cleaner_state_no_command(self) -> None:
        self.storage.save_off_period(OffPeriod(
            id="op-idle",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        ))
        for st in ("idle", "charged", "charging", "homing", "paused"):
            self.client.status = DeviceStatus(
                device_id=DEFAULT_DEVICE_ID, operating_state=st, battery=100, is_online=True
            )
            event = self.watcher.step(self.base_time)
            self.assertIsNone(event)
            self.assertEqual(len(self.client.return_to_home_calls), 0)

    def test_scheduler_step_triggers_auto_stop(self) -> None:
        self.storage.save_off_period(OffPeriod(
            id="op-sched",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        ))
        self.client.status = DeviceStatus(
            device_id=DEFAULT_DEVICE_ID, operating_state="cleaning", battery=100, is_online=True
        )
        scheduler = ReservationScheduler(
            storage=self.storage,
            client=self.client,  # type: ignore[arg-type]
            device_id=DEFAULT_DEVICE_ID,
            now_func=lambda: self.base_time,
            auto_stop_watcher=self.watcher,
        )
        scheduler.step()
        self.assertEqual(self.client.return_to_home_calls, [DEFAULT_DEVICE_ID])
        self.assertEqual(len(self.storage.list_auto_stop_events()), 1)

    def test_file_line_limits(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for rel, limit in [
            ("server/off_periods/auto_stop.py", 200),
            ("server/scheduler/scheduler.py", 300),
            ("tests/test_auto_stop.py", 250),
        ]:
            lines = (root / rel).read_text(encoding="utf-8").splitlines()
            self.assertLessEqual(len(lines), limit, f"{rel} exceeds {limit} lines (has {len(lines)})")
