from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import STATUS_FAILED, STATUS_SCHEDULED, STATUS_SUCCESS, Reservation
from server.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer
from server.smartthings.client import DeviceStatus
from server.storage import Storage
from server.time_utils import KST, format_kst

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ITEM_KT = PROJECT_ROOT / "android/app/src/main/java/com/smartthings/cleaner/model/ReservationItem.kt"


class MockClient:
    def __init__(self, operating_state: str = "idle", movement: str = "") -> None:
        self.operating_state = operating_state
        self.movement = movement
        self.started: list[str] = []

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        self.started.append(device_id)
        return {"results": [{"id": f"cmd-{device_id}", "status": "COMPLETED"}]}

    def get_device_status(self, device_id: str) -> DeviceStatus:
        return DeviceStatus(
            device_id=device_id,
            operating_state=self.operating_state,
            raw_status={
                "components": {
                    "main": {
                        "robotCleanerOperatingState": {"operatingState": {"value": self.operating_state}},
                        "robotCleanerMovement": {"movement": {"value": self.movement}},
                    }
                }
            },
        )


class MockSocket:
    def __init__(self, data: bytes) -> None:
        self.rfile, self.wfile = io.BytesIO(data), io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


def _http_get(server: MiniPCServer, path: str, secret: str) -> tuple[int, dict[str, Any]]:
    raw = f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Shared-Secret: {secret}\r\n\r\n"
    sock = MockSocket(raw.encode("utf-8"))
    MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
    parts = sock.wfile.getvalue().split(b"\r\n\r\n", 1)
    code = int(parts[0].split(b"\r\n")[0].split(b" ")[1])
    body = json.loads(parts[1].decode("utf-8")) if len(parts) > 1 and parts[1] else {}
    return code, body


class AndroidReservationItem:
    def __init__(self, status: str, last_device_state: str | None) -> None:
        self.status = status
        self.last_device_state = last_device_state

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AndroidReservationItem:
        return cls(status=str(data.get("status", STATUS_SCHEDULED)), last_device_state=data.get("last_device_state"))

    def is_upcoming(self) -> bool:
        return self.status == STATUS_SCHEDULED


class TestSchedulerSuccess(unittest.TestCase):
    """LB-017: 예약 시각 ±5분 내 청소 시작 확인 후 '성공' 표시 단위 테스트."""

    def setUp(self) -> None:
        self.base_time = datetime(2026, 10, 31, 14, 0, 0, tzinfo=KST)
        self.current_time = self.base_time
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_success.db"
        self.log_path = Path(self.temp_dir.name) / "scheduler.log"
        self.storage = Storage(self.db_path)
        self.client = MockClient(operating_state="idle")
        self.scheduler = ReservationScheduler(
            storage=self.storage,
            client=self.client,  # type: ignore[arg-type]
            device_id="test-cleaner-vr7m",
            poll_interval=0.05,
            now_func=lambda: self.current_time,
            log_file=self.log_path,
        )

    def tearDown(self) -> None:
        if self.scheduler.is_running:
            self.scheduler.stop(timeout=1.0)
        self.storage.close()
        logger = logging.getLogger("server.scheduler")
        for h in list(logger.handlers):
            if isinstance(h, logging.FileHandler):
                h.flush()
                h.close()
                logger.removeHandler(h)
        self.temp_dir.cleanup()

    def test_transitions_to_success_on_cleaning_and_processing(self) -> None:
        """a) 예약 실행 후 cleaning 또는 processing 상태 확인 시 '성공' 전이 및 last_device_state 저장."""
        res = Reservation(id="res-01", reserved_at=self.base_time, status=STATUS_SCHEDULED)
        self.storage.save_reservation(res)
        self.scheduler.step()
        self.assertEqual(len(self.client.started), 1)

        self.client.operating_state = "cleaning"
        self.current_time = self.base_time + timedelta(minutes=1)
        self.scheduler.step()

        updated = self.storage.get_reservation("res-01")
        assert updated is not None
        self.assertEqual(updated.status, STATUS_SUCCESS)
        self.assertEqual(updated.last_device_state, "cleaning")
        self.assertEqual(updated.updated_at, self.current_time)

        res2 = Reservation(id="res-02", reserved_at=self.base_time, status=STATUS_SCHEDULED)
        self.storage.save_reservation(res2)
        self.client.operating_state = "processing"
        self.scheduler.step()
        updated2 = self.storage.get_reservation("res-02")
        assert updated2 is not None
        self.assertEqual(updated2.status, STATUS_SUCCESS)
        self.assertEqual(updated2.last_device_state, "processing")

    def test_server_api_and_android_contract(self) -> None:
        """b, c) GET /api/reservations 응답 '성공' 반환 및 Android ReservationItem 파싱 검증."""
        res = Reservation(id="r-api", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time)
        self.storage.save_reservation(res)
        self.client.operating_state = "cleaning"
        self.scheduler.step()

        config = Config(host="127.0.0.1", port=0, shared_secret="secret-key", db_path=self.db_path)
        server = MiniPCServer(config, self.storage, scheduler=self.scheduler)
        try:
            code, body = _http_get(server, "/api/reservations", "secret-key")
            self.assertEqual(code, 200)
            target = next(i for i in body.get("reservations", []) if i["id"] == "r-api")
            self.assertEqual(target["status"], "성공")
            self.assertEqual(target["last_device_state"], "cleaning")

            android_item = AndroidReservationItem.from_json(target)
            self.assertEqual(android_item.status, "성공")
            self.assertFalse(android_item.is_upcoming())

            kt = ANDROID_ITEM_KT.read_text(encoding="utf-8")
            self.assertIn('const val STATUS_SUCCESS = "성공"', kt)
            self.assertIn("fun isUpcoming(): Boolean = status == STATUS_SCHEDULED", kt)
        finally:
            server.server_close()

    def test_device_not_yet_cleaning_remains_scheduled_in_window(self) -> None:
        """d) ±5분 내 아직 idle 또는 charging이면 '예정' 상태를 유지한다."""
        res = Reservation(id="r-wait", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time)
        self.storage.save_reservation(res)

        for state in ("idle", "charging"):
            self.client.operating_state = state
            self.current_time = self.base_time + timedelta(minutes=2)
            self.scheduler.step()
            item = self.storage.get_reservation("r-wait")
            assert item is not None
            self.assertEqual(item.status, STATUS_SCHEDULED)

    def test_timing_within_and_outside_window(self) -> None:
        """e) 예약 시각 ±5분 범위(-5분, 0분, +5분) 성공 판정 및 초과 시 미판정 검증."""
        # -2분 시점 청소 시작 확인
        r1 = Reservation(
            id="r1",
            reserved_at=self.base_time + timedelta(minutes=2),
            status=STATUS_SCHEDULED,
            executed_at=self.base_time,
        )
        self.storage.save_reservation(r1)
        self.client.operating_state = "cleaning"
        self.scheduler.step()
        self.assertEqual(self.storage.get_reservation("r1").status, STATUS_SUCCESS)  # type: ignore[union-attr]

        # +4분 시점 청소 시작 확인
        r2 = Reservation(id="r2", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time)
        self.storage.save_reservation(r2)
        self.current_time = self.base_time + timedelta(minutes=4)
        self.scheduler.step()
        self.assertEqual(self.storage.get_reservation("r2").status, STATUS_SUCCESS)  # type: ignore[union-attr]

        # +6분 시점(초과) 창 초과로 실패 전이 (LB-026)
        r3 = Reservation(id="r3", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time)
        self.storage.save_reservation(r3)
        self.current_time = self.base_time + timedelta(minutes=6)
        self.scheduler.step()
        self.assertEqual(self.storage.get_reservation("r3").status, STATUS_FAILED)  # type: ignore[union-attr]

    def test_movement_cleaning_marks_success(self) -> None:
        """operating_state가 idle이어도 movement가 cleaning이면 '성공' 판정."""
        res = Reservation(id="r-mv", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time)
        self.storage.save_reservation(res)
        self.client.operating_state, self.client.movement = "idle", "cleaning"
        self.scheduler.step()
        updated = self.storage.get_reservation("r-mv")
        assert updated is not None
        self.assertEqual(updated.status, STATUS_SUCCESS)

    def test_scheduler_log_file_output(self) -> None:
        """f) server/logs/scheduler.log 에 KST 타임스탬프와 확인 로그 기록 검증."""
        res = Reservation(id="r-lg", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time)
        self.storage.save_reservation(res)
        self.client.operating_state = "cleaning"
        self.scheduler.step()

        for h in self.scheduler.logger.handlers:
            h.flush()
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("청소 시작 확인 완료 (성공)", content)
        self.assertIn("r-lg", content)
        self.assertIn("cleaning", content)
        self.assertIn(format_kst(self.base_time), content)


if __name__ == "__main__":
    unittest.main()
