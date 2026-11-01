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
from server.models import STATUS_MISSED, STATUS_SCHEDULED, Reservation
from server.scheduler import ReservationScheduler, get_scheduler_logger
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage
from server.time_utils import KST

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ITEM_KT = PROJECT_ROOT / "android/app/src/main/java/com/smartthings/cleaner/model/ReservationItem.kt"


class DummyCleanerClient:
    """테스트용 스마트싱스 클라이언트."""

    def __init__(self) -> None:
        self.started_devices: list[str] = []
        self.call_count: int = 0

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        self.call_count += 1
        self.started_devices.append(device_id)
        return {"results": [{"id": f"cmd-{device_id}", "status": "COMPLETED"}]}


class MockSocket:
    """인메모리 HTTP 요청 소켓."""

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
    """Android ReservationItem.fromJson() 파싱 및 모델 미러."""

    def __init__(self, item_id: str, status: str, failure_reason: str | None = None) -> None:
        self.id = item_id
        self.status = status
        self.failure_reason = failure_reason

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> AndroidReservationItem:
        return cls(
            item_id=str(data.get("id", "")),
            status=str(data.get("status", STATUS_SCHEDULED)),
            failure_reason=data.get("failure_reason"),
        )

    def is_upcoming(self) -> bool:
        return self.status == STATUS_SCHEDULED


def _cleanup_logger_handlers(log_path: Path) -> None:
    logger = logging.getLogger("server.scheduler")
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == log_path.resolve():
            h.flush()
            h.close()
            logger.removeHandler(h)


class TestSchedulerMissed(unittest.TestCase):
    """LB-028: 놓친 예약 '누락' 처리 및 재부팅 검증 (REQ-027, D-08, D-10)."""

    def setUp(self) -> None:
        self.t_0 = datetime(2026, 11, 1, 10, 0, 0, tzinfo=KST)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_missed.db"
        self.log_path = Path(self.temp_dir.name) / "scheduler.log"
        get_scheduler_logger(self.log_path)

    def tearDown(self) -> None:
        _cleanup_logger_handlers(self.log_path)
        self.temp_dir.cleanup()

    def test_done_when_reboot_scenario_marks_missed_and_never_cleans(self) -> None:
        """done_when: 서버 꺼둔 채 예약 시각 +5분 초과 후 재부팅 시 청소기 미출발 및 '누락' 표시."""
        res_id = "res-missed-reboot"
        storage1 = Storage(self.db_path)
        storage1.save_reservation(Reservation(id=res_id, reserved_at=self.t_0, status=STATUS_SCHEDULED))
        storage1.close()

        # 시계 T_0 + 6분 경과 후 서버 2 가동 (동일 SQLite DB)
        t_reboot = self.t_0 + timedelta(minutes=6)
        storage2 = Storage(self.db_path)
        client2 = DummyCleanerClient()
        sched2 = ReservationScheduler(
            storage=storage2, client=client2, device_id="vr7md97716g-cleaner",  # type: ignore[arg-type]
            now_func=lambda: t_reboot, log_file=self.log_path,
        )
        executed = sched2.step()

        # Must: 청소기 출발 명령이 절대 호출되지 않음
        self.assertEqual(executed, [])
        self.assertEqual(client2.call_count, 0)
        self.assertEqual(client2.started_devices, [])

        # DB 상태 확인
        saved = storage2.get_reservation(res_id)
        assert saved is not None
        self.assertEqual(saved.status, STATUS_MISSED)
        self.assertEqual(saved.failure_reason, "예약 시각 5분 경과로 누락 처리됨")
        self.assertIsNone(saved.executed_at)
        self.assertEqual(saved.updated_at, t_reboot)

        # HTTP GET /api/reservations 응답 status == '누락' 확인
        cfg2 = Config(host="127.0.0.1", port=0, shared_secret="secret-key", db_path=self.db_path)
        server2 = MiniPCServer(cfg2, storage2, scheduler=sched2)
        code, body = _http_get(server2, "/api/reservations", "secret-key")
        self.assertEqual(code, 200)
        item = next(i for i in body.get("reservations", []) if i["id"] == res_id)
        self.assertEqual(item["status"], "누락")
        self.assertEqual(item["failure_reason"], "예약 시각 5분 경과로 누락 처리됨")

        # Android ReservationItem.fromJson() 파싱 및 isUpcoming() == False 확인
        android_item = AndroidReservationItem.from_json(item)
        self.assertEqual(android_item.status, "누락")
        self.assertFalse(android_item.is_upcoming())

        kt_source = ANDROID_ITEM_KT.read_text(encoding="utf-8")
        self.assertIn('const val STATUS_MISSED = "누락"', kt_source)
        self.assertIn("fun isUpcoming(): Boolean = status == STATUS_SCHEDULED", kt_source)

        # scheduler.log: 누락 로그 기록 확인 및 청소 시작 명령 전송 0건 확인
        _cleanup_logger_handlers(self.log_path)
        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn(f"예약 누락 처리 (5분 경과): id={res_id}", log_text)
        self.assertNotIn("청소 시작 명령 전송 시도", log_text)
        self.assertNotIn("청소 시작 명령 전송 성공", log_text)

        server2.server_close()
        storage2.close()

    def test_boundary_condition_within_5m_vs_past_5m(self) -> None:
        """경계값 검증: 5분 이내(+4분)는 정상 출발, 5분 초과(+5분 1초)는 즉시 '누락' 처리 (Must)."""
        storage, client = Storage(self.db_path), DummyCleanerClient()

        # 5분 이내(+4분): 정상 실행
        storage.save_reservation(Reservation(id="res-within", reserved_at=self.t_0, status=STATUS_SCHEDULED))
        sched_within = ReservationScheduler(
            storage=storage, client=client, now_func=lambda: self.t_0 + timedelta(minutes=4),  # type: ignore[arg-type]
            log_file=self.log_path,
        )
        self.assertEqual([r.id for r in sched_within.step()], ["res-within"])
        self.assertEqual(client.started_devices, ["03431570-5acf-f89e-7b39-275ce15d3258"])

        # 5분 초과(+5분 1초): 누락 처리 및 미출발
        t_1 = self.t_0 + timedelta(hours=1)
        storage.save_reservation(Reservation(id="res-past", reserved_at=t_1, status=STATUS_SCHEDULED))
        sched_past = ReservationScheduler(
            storage=storage, client=client, now_func=lambda: t_1 + timedelta(minutes=5, seconds=1),  # type: ignore[arg-type]
            log_file=self.log_path,
        )
        self.assertEqual(sched_past.step(), [])
        self.assertEqual(client.call_count, 1)

        saved_past = storage.get_reservation("res-past")
        assert saved_past is not None
        self.assertEqual(saved_past.status, STATUS_MISSED)
        self.assertEqual(saved_past.failure_reason, "예약 시각 5분 경과로 누락 처리됨")
        self.assertIsNone(saved_past.executed_at)
        storage.close()

    def test_mixed_reservations_after_server_recovery(self) -> None:
        """복구 후 혼합 예약: 10분 지남('누락'), 현재 도래(실행), 미래(+30분, '예정' 유지)."""
        storage = Storage(self.db_path)
        client = DummyCleanerClient()
        r_missed = Reservation(id="r-old", reserved_at=self.t_0 - timedelta(minutes=10), status=STATUS_SCHEDULED)
        r_due = Reservation(id="r-due", reserved_at=self.t_0, status=STATUS_SCHEDULED)
        r_future = Reservation(id="r-next", reserved_at=self.t_0 + timedelta(minutes=30), status=STATUS_SCHEDULED)

        for r in (r_missed, r_due, r_future):
            storage.save_reservation(r)

        sched = ReservationScheduler(
            storage=storage, client=client, now_func=lambda: self.t_0, log_file=self.log_path  # type: ignore[arg-type]
        )
        executed = sched.step()
        self.assertEqual([r.id for r in executed], ["r-due"])
        self.assertEqual(client.call_count, 1)

        db_missed = storage.get_reservation("r-old")
        db_due = storage.get_reservation("r-due")
        db_future = storage.get_reservation("r-next")
        assert db_missed and db_due and db_future

        self.assertEqual(db_missed.status, STATUS_MISSED)
        self.assertEqual(db_missed.failure_reason, "예약 시각 5분 경과로 누락 처리됨")
        self.assertIsNone(db_missed.executed_at)

        self.assertEqual(db_due.status, STATUS_SCHEDULED)
        self.assertEqual(db_due.executed_at, self.t_0)

        self.assertEqual(db_future.status, STATUS_SCHEDULED)
        self.assertIsNone(db_future.executed_at)

        # Must 규칙: 누락된 예약은 이후 step에서도 절대 실행되지 않음
        executed_second = sched.step()
        self.assertEqual(executed_second, [])
        self.assertEqual(client.call_count, 1)
        storage.close()

    def test_file_line_counts(self) -> None:
        """파일 크기 규칙 검증: 300줄 이하 (테스트 파일은 250줄 이하)."""
        test_file = Path(__file__)
        self.assertLessEqual(len(test_file.read_text(encoding="utf-8").splitlines()), 250)
        sched_file = PROJECT_ROOT / "server/scheduler/scheduler.py"
        self.assertLessEqual(len(sched_file.read_text(encoding="utf-8").splitlines()), 300)


if __name__ == "__main__":
    unittest.main()
