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
from server.models import STATUS_FAILED, STATUS_SCHEDULED, Reservation
from server.notify import FCMClient, NotificationService
from server.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer
from server.smartthings.client import DeviceStatus, SmartThingsApiError
from server.storage import Storage
from server.time_utils import KST

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MockCleanerClient:
    def __init__(
        self,
        operating_state: str = "idle",
        is_online: bool = True,
        battery: int = 100,
        dust_bag_status: str = "normal",
        health: str = "ONLINE",
        raise_status: Exception | None = None,
    ) -> None:
        self.operating_state = operating_state
        self.is_online = is_online
        self.battery = battery
        self.dust_bag_status = dust_bag_status
        self.health = health
        self.raise_status = raise_status
        self.started: list[str] = []

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        self.started.append(device_id)
        return {"results": [{"id": f"cmd-{device_id}", "status": "COMPLETED"}]}

    def get_device_health(self, device_id: str) -> str:
        return self.health

    def get_device_status(self, device_id: str) -> DeviceStatus:
        if self.raise_status:
            raise self.raise_status
        return DeviceStatus(
            device_id=device_id,
            operating_state=self.operating_state,
            battery=self.battery,
            is_online=self.is_online,
            dust_bag_status=self.dust_bag_status,
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


class TestSchedulerFailure(unittest.TestCase):
    """LB-026: 청소 미시작 시 '실패' 표시와 실패 푸시 알림 (REQ-008, REQ-009, REQ-022, D-10, D-12)."""

    def setUp(self) -> None:
        self.base_time = datetime(2026, 10, 31, 14, 0, 0, tzinfo=KST)
        self.current_time = self.base_time
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_failure.db"
        self.log_path = Path(self.temp_dir.name) / "scheduler.log"
        self.storage = Storage(self.db_path)
        self.fcm = FCMClient(simulated=True)
        self.notify_service = NotificationService(self.storage, self.fcm)
        self.notify_service.register_device("galaxy-s21-plus", "token-s21", target_id="s21")
        self.notify_service.register_device("galaxy-fold-8", "token-fold8", target_id="fold8")
        self.client = MockCleanerClient()
        self.scheduler = ReservationScheduler(
            storage=self.storage,
            client=self.client,  # type: ignore[arg-type]
            device_id="test-cleaner-vr7m",
            poll_interval=0.05,
            now_func=lambda: self.current_time,
            log_file=self.log_path,
            notification_service=self.notify_service,
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

    def test_power_off_offline_scenario_done_when(self) -> None:
        """청소기 전원 꺼짐/오프라인 시 +5분 후 '실패' 전이, 원인 표시, 두 기기 푸시 발송 (done_when)."""
        self.client.raise_status = SmartThingsApiError("기기 오프라인 (전원 꺼짐)")
        self.client.health = "OFFLINE"
        res = Reservation(
            id="res-fail-01", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time
        )
        self.storage.save_reservation(res)

        # 1. ±5분 창 내부 (+4분): 아직 실패로 전이되지 않음 (REQ-007 창 대기)
        self.current_time = self.base_time + timedelta(minutes=4)
        self.scheduler.step()
        r_mid = self.storage.get_reservation("res-fail-01")
        assert r_mid is not None
        self.assertEqual(r_mid.status, STATUS_SCHEDULED)
        self.assertEqual(len(self.fcm.dispatched_messages), 0)

        # 2. +5분 1초 초과 시점: 실패 전이 및 푸시 알림 발송 (REQ-008, REQ-009, D-10, D-12)
        self.current_time = self.base_time + timedelta(minutes=5, seconds=1)
        self.scheduler.step()

        r_failed = self.storage.get_reservation("res-fail-01")
        assert r_failed is not None
        self.assertEqual(r_failed.status, STATUS_FAILED)
        self.assertEqual(r_failed.failure_reason, "기기 오프라인")
        self.assertEqual(r_failed.updated_at, self.current_time)

        # 3. 등록된 두 기기(S21+, 폴드8) 모두에 실패 알림 푸시 전송 검증 (REQ-022, D-12)
        self.assertEqual(len(self.fcm.dispatched_messages), 2)
        tokens = {m["push_token"] for m in self.fcm.dispatched_messages}
        self.assertEqual(tokens, {"token-s21", "token-fold8"})
        for m in self.fcm.dispatched_messages:
            self.assertEqual(m["title"], "[로봇청소기] 청소 실패")
            self.assertIn("예약 청소가 시작되지 않았습니다: 기기 오프라인", m["body"])

        # 4. 안드로이드 API GET /api/reservations 응답 상태 '실패' 및 failure_reason 검증
        cfg = Config(host="127.0.0.1", port=0, shared_secret="secret-key", db_path=self.db_path)
        server = MiniPCServer(cfg, self.storage, scheduler=self.scheduler)
        try:
            code, body = _http_get(server, "/api/reservations", "secret-key")
            self.assertEqual(code, 200)
            item = next(i for i in body.get("reservations", []) if i["id"] == "res-fail-01")
            self.assertEqual(item["status"], "실패")
            self.assertEqual(item["failure_reason"], "기기 오프라인")
        finally:
            server.server_close()

        # 5. scheduler.log 기록 검증
        for h in self.scheduler.logger.handlers:
            h.flush()
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("청소 시작 확인 실패", content)
        self.assertIn("res-fail-01", content)
        self.assertIn("기기 오프라인", content)

    def test_push_sent_only_once_on_subsequent_steps(self) -> None:
        """실패 전이 후 추가 스케줄러 step 반복 호출 시 알림이 중복 전송되지 않는다."""
        self.client.is_online = False
        res = Reservation(
            id="res-fail-02", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time
        )
        self.storage.save_reservation(res)

        self.current_time = self.base_time + timedelta(minutes=5, seconds=1)
        self.scheduler.step()
        self.assertEqual(len(self.fcm.dispatched_messages), 2)

        # 후속 주기 실행 시 알림 재발송 없음
        for m in (6, 7, 10):
            self.current_time = self.base_time + timedelta(minutes=m)
            self.scheduler.step()
            self.assertEqual(len(self.fcm.dispatched_messages), 2)

    def test_failure_reasons_battery_and_unknown(self) -> None:
        """배터리 부족, 먼지통 가득 참 및 원인 미확인(마지막 상태) 분석 검증 (REQ-009)."""
        # 배터리 부족 시나리오
        self.client.battery = 15
        r_bat = Reservation(id="r-bat", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time)
        self.storage.save_reservation(r_bat)
        self.current_time = self.base_time + timedelta(minutes=5, seconds=1)
        self.scheduler.step()
        item_bat = self.storage.get_reservation("r-bat")
        assert item_bat is not None
        self.assertEqual(item_bat.status, STATUS_FAILED)
        self.assertEqual(item_bat.failure_reason, "배터리 부족 (15%)")

        # 원인 미확인 + 마지막 상태 시나리오
        self.client.battery = 100
        self.client.operating_state = "idle"
        r_unk = Reservation(id="r-unk", reserved_at=self.base_time, status=STATUS_SCHEDULED, executed_at=self.base_time)
        self.storage.save_reservation(r_unk)
        self.scheduler.step()
        item_unk = self.storage.get_reservation("r-unk")
        assert item_unk is not None
        self.assertEqual(item_unk.status, STATUS_FAILED)
        self.assertEqual(item_unk.failure_reason, "원인 미확인 (마지막 상태: idle)")
        self.assertEqual(item_unk.last_device_state, "idle")

    def test_file_line_limits(self) -> None:
        """모든 관련 파일 크기가 300줄 이하인지 검증."""
        files = [
            PROJECT_ROOT / "server/scheduler/scheduler.py",
            PROJECT_ROOT / "server/server.py",
            PROJECT_ROOT / "server/notify/service.py",
            Path(__file__),
        ]
        for f in files:
            lines = len(f.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 300, f"{f.name} exceeds 300 lines: {lines}")


if __name__ == "__main__":
    unittest.main()
