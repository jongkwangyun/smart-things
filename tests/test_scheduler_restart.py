from __future__ import annotations

import logging
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import (
    STATUS_SCHEDULED,
    Reservation,
)
from server.scheduler import (
    ReservationScheduler,
    get_scheduler_logger,
)
from server.server import MiniPCServer
from server.smartthings.client import SmartThingsApiError
from server.storage import Storage
from server.time_utils import KST


class DummyRestartClient:
    """재시작 검증용 가짜 스마트싱스 클라이언트."""

    def __init__(self) -> None:
        self.started_devices: list[str] = []
        self.call_count: int = 0
        self.should_raise: Exception | None = None

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        self.call_count += 1
        if self.should_raise:
            raise self.should_raise
        self.started_devices.append(device_id)
        return {"results": [{"id": f"cmd-{device_id}", "status": "COMPLETED"}]}


def _flush_logger(log_path: Path) -> None:
    logger = logging.getLogger("server.scheduler")
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == log_path.resolve():
            h.flush()


def _cleanup_logger_handlers(log_path: Path) -> None:
    logger = logging.getLogger("server.scheduler")
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == log_path.resolve():
            h.flush()
            h.close()
            logger.removeHandler(h)


class TestSchedulerRestart(unittest.TestCase):
    """LB-016: 1회성 예약 1회 실행 보장 및 서버 재시작 검증 (REQ-006, REQ-026)."""

    def setUp(self) -> None:
        self.base_time = datetime(2026, 10, 31, 15, 0, 0, tzinfo=KST)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "restart_test.db"
        self.log_path = Path(self.temp_dir.name) / "scheduler_restart.log"
        get_scheduler_logger(self.log_path)

    def tearDown(self) -> None:
        _cleanup_logger_handlers(self.log_path)
        self.temp_dir.cleanup()

    def _create_scheduler(
        self,
        storage: Storage,
        client: DummyRestartClient,
        now: datetime,
        poll_interval: float = 1.0,
    ) -> ReservationScheduler:
        return ReservationScheduler(
            storage=storage,
            client=client,  # type: ignore[arg-type]
            device_id="vr7md97716g-device",
            poll_interval=poll_interval,
            now_func=lambda: now,
            log_file=self.log_path,
        )

    def _count_log_patterns(self, pattern: str) -> int:
        _flush_logger(self.log_path)
        content = self.log_path.read_text(encoding="utf-8")
        return sum(1 for line in content.splitlines() if pattern in line)

    def test_done_when_no_duplicate_cleaning_command_after_restart(self) -> None:
        """done_when: 예약 실행 직후 서버를 재시작해도 청소 명령이 다시 나가지 않음을 로그로 검증."""
        res_id = "res-restart-01"
        res_time = self.base_time - timedelta(minutes=1)

        # 1. 서버 1 초기화 및 예약 1회 실행
        storage1 = Storage(self.db_path)
        client1 = DummyRestartClient()
        sched1 = self._create_scheduler(storage1, client1, self.base_time)

        storage1.save_reservation(Reservation(id=res_id, reserved_at=res_time, status=STATUS_SCHEDULED))
        executed1 = sched1.step()

        self.assertEqual([r.id for r in executed1], [res_id])
        self.assertEqual(client1.call_count, 1)
        self.assertEqual(client1.started_devices, ["vr7md97716g-device"])

        # 영구 저장소에 executed_at 저장 확인 (Must)
        saved = storage1.get_reservation(res_id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.executed_at, self.base_time)

        # 2. 서버 1 중지 (재부팅/재시작)
        sched1.stop()
        storage1.close()

        # 3. 서버 2 시작 (동일한 DB 및 동일한 로그 파일)
        storage2 = Storage(self.db_path)
        client2 = DummyRestartClient()
        sched2 = self._create_scheduler(storage2, client2, self.base_time + timedelta(minutes=2))

        # 서버 2에서 반복 폴링 실행
        executed2_first = sched2.step()
        executed2_second = sched2.step()

        # 4. 검증: 서버 2에서는 청소 시작 명령이 전송되지 않음
        self.assertEqual(executed2_first, [])
        self.assertEqual(executed2_second, [])
        self.assertEqual(client2.call_count, 0)
        self.assertEqual(client2.started_devices, [])

        # 5. 서버 로그 확인 (done_when 필수 조건)
        attempt_count = self._count_log_patterns(f"청소 시작 명령 전송 시도: res_id={res_id}")
        success_count = self._count_log_patterns(f"청소 시작 명령 전송 성공: res_id={res_id}")
        self.assertEqual(attempt_count, 1)
        self.assertEqual(success_count, 1)

        sched2.stop()
        storage2.close()

    def test_restart_mixed_executed_and_pending_reservations(self) -> None:
        """실행 완료된 예약(A)과 대기 중인 예약(B)이 혼합된 상태에서 재시작 후 정확히 1회 실행 검증."""
        id_a, id_b = "res-mixed-a", "res-mixed-b"
        time_a = self.base_time - timedelta(minutes=2)
        time_b = self.base_time + timedelta(minutes=3)

        # 1. 서버 1에서 A, B 등록 후 실행 (A만 실행)
        storage1 = Storage(self.db_path)
        client1 = DummyRestartClient()
        sched1 = self._create_scheduler(storage1, client1, self.base_time)
        storage1.save_reservation(Reservation(id=id_a, reserved_at=time_a, status=STATUS_SCHEDULED))
        storage1.save_reservation(Reservation(id=id_b, reserved_at=time_b, status=STATUS_SCHEDULED))

        executed1 = sched1.step()
        self.assertEqual([r.id for r in executed1], [id_a])
        self.assertEqual(client1.call_count, 1)

        sched1.stop()
        storage1.close()

        # 2. 서버 2 시작 (B의 예약 시각 도래: base_time + 4분)
        t_reboot = self.base_time + timedelta(minutes=4)
        storage2 = Storage(self.db_path)
        client2 = DummyRestartClient()
        sched2 = self._create_scheduler(storage2, client2, t_reboot)

        executed2 = sched2.step()
        # 서버 2에서는 이미 실행된 A는 무시되고, B만 1회 실행됨
        self.assertEqual([r.id for r in executed2], [id_b])
        self.assertEqual(client2.call_count, 1)

        # DB 상태 및 로그 검증
        saved_a = storage2.get_reservation(id_a)
        saved_b = storage2.get_reservation(id_b)
        assert saved_a is not None and saved_b is not None
        self.assertEqual(saved_a.executed_at, self.base_time)
        self.assertEqual(saved_b.executed_at, t_reboot)

        self.assertEqual(self._count_log_patterns(f"청소 시작 명령 전송 시도: res_id={id_a}"), 1)
        self.assertEqual(self._count_log_patterns(f"청소 시작 명령 전송 시도: res_id={id_b}"), 1)

        sched2.stop()
        storage2.close()

    def test_reboot_with_server_daemon_thread_lifecycle(self) -> None:
        """MiniPCServer 백그라운드 스케줄러 스레드 가동 환경에서 재부팅 라이프사이클 검증."""
        res_id = "res-daemon-cycle"
        res_time = self.base_time - timedelta(seconds=1)

        # 서버 1 가동
        cfg1 = Config(host="127.0.0.1", port=0, db_path=self.db_path)
        storage1 = Storage(self.db_path)
        client1 = DummyRestartClient()
        sched1 = self._create_scheduler(storage1, client1, self.base_time, poll_interval=0.05)
        server1 = MiniPCServer(config=cfg1, storage=storage1, scheduler=sched1)

        storage1.save_reservation(Reservation(id=res_id, reserved_at=res_time, status=STATUS_SCHEDULED))
        sched1.start()

        for _ in range(20):
            if client1.call_count > 0:
                break
            time.sleep(0.05)

        self.assertEqual(client1.call_count, 1)
        server1.close()
        self.assertFalse(sched1.is_running)

        # 서버 2 가동 (동일 DB, 동일 로그)
        cfg2 = Config(host="127.0.0.1", port=0, db_path=self.db_path)
        storage2 = Storage(self.db_path)
        client2 = DummyRestartClient()
        sched2 = self._create_scheduler(
            storage2, client2, self.base_time + timedelta(minutes=1), poll_interval=0.05
        )
        server2 = MiniPCServer(config=cfg2, storage=storage2, scheduler=sched2)
        sched2.start()

        time.sleep(0.15)
        self.assertEqual(client2.call_count, 0)

        server2.close()
        self.assertFalse(sched2.is_running)

        self.assertEqual(self._count_log_patterns(f"청소 시작 명령 전송 시도: res_id={res_id}"), 1)

    def test_must_executed_at_saved_before_command_prevents_duplicate_on_failure(self) -> None:
        """Must 규칙: 명령 전송 실패 시에도 executed_at이 먼저 영구 저장되어 재시작 시 중복 명령 전송 방지."""
        res_id = "res-fail-crash"
        res_time = self.base_time - timedelta(seconds=10)

        storage1 = Storage(self.db_path)
        client1 = DummyRestartClient()
        client1.should_raise = SmartThingsApiError("스마트싱스 통신 일시 장애")
        sched1 = self._create_scheduler(storage1, client1, self.base_time)

        storage1.save_reservation(Reservation(id=res_id, reserved_at=res_time, status=STATUS_SCHEDULED))
        sched1.step()

        # executed_at이 저장되었는지 확인
        saved1 = storage1.get_reservation(res_id)
        assert saved1 is not None
        self.assertEqual(saved1.executed_at, self.base_time)
        self.assertIn("명령 전송 실패", str(saved1.failure_reason))

        sched1.stop()
        storage1.close()

        # 서버 2 재부팅 후 가동
        storage2 = Storage(self.db_path)
        client2 = DummyRestartClient()
        sched2 = self._create_scheduler(storage2, client2, self.base_time + timedelta(minutes=1))

        executed2 = sched2.step()
        self.assertEqual(executed2, [])
        self.assertEqual(client2.call_count, 0)
        self.assertEqual(self._count_log_patterns(f"res_id={res_id}"), 2)  # 시도 1회 + 실패 1회만 존재

        sched2.stop()
        storage2.close()
