from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import (
    STATUS_FAILED,
    STATUS_SCHEDULED,
    STATUS_SUCCESS,
    OffPeriod,
    Reservation,
)
from server.notify import FCMClient, NotificationService
from server.off_periods.auto_stop import DEFAULT_DEVICE_ID, AutoStopWatcher
from server.scheduler.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer
from server.smartthings.client import DeviceStatus
from server.storage import Storage
from server.time_utils import format_kst, now_kst

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ITEM_KT = PROJECT_ROOT / "android/app/src/main/java/com/smartthings/cleaner/model/ReservationItem.kt"


class MockSocket:
    def __init__(self, data: bytes) -> None:
        self.rfile, self.wfile = io.BytesIO(data), io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


def http_req(
    srv: MiniPCServer, method: str, path: str, body: dict[str, Any] | None = None, secret: str = "sec"
) -> tuple[int, dict[str, Any]]:
    payload = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", f"X-Shared-Secret: {secret}"]
    if body is not None:
        headers.extend(["Content-Type: application/json", f"Content-Length: {len(payload)}"])
    sock = MockSocket("\r\n".join(headers).encode("utf-8") + b"\r\n\r\n" + payload)
    MiniPCHandler(sock, ("127.0.0.1", 12345), srv)  # type: ignore[arg-type]
    parts = sock.wfile.getvalue().split(b"\r\n\r\n", 1)
    code = int(parts[0].split(b"\r\n")[0].split(b" ")[1])
    data = json.loads(parts[1].decode("utf-8")) if len(parts) > 1 and parts[1] else {}
    return code, data


class MockClient:
    def __init__(self, state: str = "idle") -> None:
        self.state = state
        self.start_calls: list[str] = []
        self.return_calls: list[str] = []

    def get_device_status(self, device_id: str) -> DeviceStatus:
        return DeviceStatus(device_id=device_id, operating_state=self.state, battery=100, is_online=True)

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        self.start_calls.append(device_id)
        self.state = "cleaning"
        return {"results": [{"id": "cmd-start", "status": "COMPLETED"}]}

    def return_to_home(self, device_id: str) -> dict[str, Any]:
        self.return_calls.append(device_id)
        self.state = "homing"
        return {"results": [{"id": "cmd-return", "status": "COMPLETED"}]}


class AndroidReservationItem:
    def __init__(self, status: str, allow_during_off_period: bool) -> None:
        self.status = status
        self.allow_during_off_period = allow_during_off_period

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AndroidReservationItem:
        return cls(
            status=str(data.get("status", STATUS_SCHEDULED)),
            allow_during_off_period=bool(data.get("allow_during_off_period", False)),
        )

    def is_upcoming(self) -> bool:
        return self.status == STATUS_SCHEDULED


class TestAllowedReservationExecution(unittest.TestCase):
    """LB-031: 쉬는 기간 중 허용된 1회성 예약 청소 실행 및 자동 정지 제외 검증 (REQ-015, D-05)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.storage = Storage(self.db_path)
        self.fcm = FCMClient(simulated=True)
        self.notify = NotificationService(self.storage, self.fcm)
        self.notify.register_device("s21-plus", "token-s21")
        self.notify.register_device("fold-8", "token-fold8")
        self.client = MockClient(state="idle")
        self.current_time = (now_kst() + timedelta(days=2)).replace(second=0, microsecond=0)
        self.watcher = AutoStopWatcher(
            storage=self.storage,
            client=self.client,  # type: ignore[arg-type]
            device_id=DEFAULT_DEVICE_ID,
            notification_service=self.notify,
            now_func=lambda: self.current_time,
        )
        self.scheduler = ReservationScheduler(
            storage=self.storage,
            client=self.client,  # type: ignore[arg-type]
            device_id=DEFAULT_DEVICE_ID,
            now_func=lambda: self.current_time,
            auto_stop_watcher=self.watcher,
        )
        self.server = MiniPCServer(
            Config(host="127.0.0.1", port=0, shared_secret="sec", db_path=self.db_path),
            self.storage,
        )

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.tmp.cleanup()

    def test_allowed_reservation_execution_and_success_flow(self) -> None:
        """Done When: 쉬는 기간 중 허용 예약 청소가 시작되어 끝까지 진행되고 '성공'으로 보인다."""
        t_now = self.current_time
        # 1. 활성 쉬는 기간 등록 [T_now - 2h, T_now + 2h]
        op = self.storage.save_off_period(
            OffPeriod(id="op-1", start_at=t_now - timedelta(hours=2), end_at=t_now + timedelta(hours=2))
        )
        self.assertIsNotNone(op)

        # 2. T_now 시각에 allow_during_off_period = True 인 1회성 예약 생성
        code_p, res_p = http_req(
            self.server, "POST", "/api/reservations",
            {"scheduled_at": format_kst(t_now), "allow_during_off_period": True},
        )
        self.assertEqual(code_p, 201)
        res_id = res_p["id"]
        self.assertTrue(res_p["allow_during_off_period"])

        # 3. 시간 T_now 로 스케줄러 실행: start_cleaning 호출
        executed = self.scheduler.step()
        self.assertEqual(len(executed), 1)
        self.assertEqual(executed[0].id, res_id)
        self.assertEqual(self.client.start_calls, [DEFAULT_DEVICE_ID])

        # 4. AutoStopWatcher 점검: 활성 쉬는 기간이지만 허용 예약이므로 return_to_home 미호출, 이벤트 미기록
        self.assertIsNotNone(self.watcher.get_active_off_period(self.current_time))
        self.assertTrue(self.watcher.is_allowed_reservation_cleaning(self.current_time))
        ev = self.watcher.step()
        self.assertIsNone(ev)
        self.assertEqual(len(self.client.return_calls), 0)
        self.assertEqual(len(self.storage.list_auto_stop_events()), 0)
        self.assertEqual(self.client.state, "cleaning")

        # 5. T_now + 2m 경과: 청소 중 상태 확인 -> '성공' 전이 및 API/안드로이드 파싱 확인
        self.current_time = t_now + timedelta(minutes=2)
        self.client.state = "cleaning"
        self.scheduler.step()

        saved_r = self.storage.get_reservation(res_id)
        self.assertIsNotNone(saved_r)
        assert saved_r is not None
        self.assertEqual(saved_r.status, STATUS_SUCCESS)

        code_g, body_g = http_req(self.server, "GET", "/api/reservations")
        self.assertEqual(code_g, 200)
        target = next(r for r in body_g["reservations"] if r["id"] == res_id)
        self.assertEqual(target["status"], STATUS_SUCCESS)
        self.assertTrue(target["allow_during_off_period"])

        item = AndroidReservationItem.from_json(target)
        self.assertEqual(item.status, "성공")
        self.assertTrue(item.allow_during_off_period)
        self.assertFalse(item.is_upcoming())

        # 청소 진행 중(T_now + 40m)에도 정지되지 않고 끝까지 청소 진행
        self.current_time = t_now + timedelta(minutes=40)
        self.assertIsNone(self.watcher.step())
        self.assertEqual(len(self.client.return_calls), 0)

    def test_unallowed_repeat_schedule_in_off_period_is_stopped(self) -> None:
        """반복 예약 등으로 허용 예약 없이 시작된 청소는 즉시 정지 및 AutoStopEvent 기록 (REQ-016)."""
        t_now = self.current_time
        op = self.storage.save_off_period(
            OffPeriod(id="op-repeat", start_at=t_now - timedelta(hours=2), end_at=t_now + timedelta(hours=2))
        )
        self.client.state = "cleaning"
        self.assertFalse(self.watcher.is_allowed_reservation_cleaning(t_now))

        event = self.watcher.step()
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.off_period_id, op.id)
        self.assertTrue(event.success)
        self.assertEqual(self.client.return_calls, [DEFAULT_DEVICE_ID])
        self.assertEqual(len(self.storage.list_auto_stop_events()), 1)
        self.assertEqual(len(self.fcm.dispatched_messages), 2)

    def test_failed_or_unallowed_reservation_not_exempt(self) -> None:
        """실패 상태이거나 허용 미체크된 예약은 자동 정지 제외 대상이 아님."""
        t_now = self.current_time
        self.storage.save_off_period(
            OffPeriod(id="op-fail", start_at=t_now - timedelta(hours=1), end_at=t_now + timedelta(hours=1))
        )
        self.storage.save_reservation(Reservation(
            id="res-fail",
            reserved_at=t_now - timedelta(minutes=10),
            status=STATUS_FAILED,
            allow_during_off_period=True,
            executed_at=t_now - timedelta(minutes=10),
        ))
        self.assertFalse(self.watcher.is_allowed_reservation_cleaning(t_now))

    def test_contracts_and_file_line_limits(self) -> None:
        """Android ReservationItem 계약 및 파일 줄 수 제한 검증 (코드 <= 300, 테스트 <= 250)."""
        kt_source = ANDROID_ITEM_KT.read_text(encoding="utf-8")
        self.assertIn('STATUS_SUCCESS = "성공"', kt_source)
        self.assertIn("allow_during_off_period", kt_source)

        my_lines = len(Path(__file__).read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(my_lines, 250, f"테스트 파일 줄 수 초과: {my_lines} > 250")
        auto_stop_p = PROJECT_ROOT / "server/off_periods/auto_stop.py"
        auto_stop_lines = len(auto_stop_p.read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(auto_stop_lines, 300, f"auto_stop.py 줄 수 초과: {auto_stop_lines} > 300")
        sched_p = PROJECT_ROOT / "server/scheduler/scheduler.py"
        sched_lines = len(sched_p.read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(sched_lines, 300, f"scheduler.py 줄 수 초과: {sched_lines} > 300")


if __name__ == "__main__":
    unittest.main()
