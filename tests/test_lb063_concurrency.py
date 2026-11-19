from __future__ import annotations

import io
import json
import logging
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from server.config import Config
from server.models import OffPeriod
from server.notify import FCMClient, NotificationService
from server.off_periods.auto_stop import DEFAULT_DEVICE_ID
from server.off_periods.dnd_sync import DndSyncManager
from server.off_periods.service import OffPeriodService
from server.scheduler import ReservationScheduler
from server.scheduler.backoff import AuthBackoffTracker
from server.server import MiniPCHandler, MiniPCServer
from server.smartthings.auth import SmartThingsTokenManager
from server.smartthings.client import SmartThingsAuthError, SmartThingsClient
from server.smartthings.health import apply_dnd_sync_outcome, record_server_dnd_outcome
from server.storage import Storage
from server.time_utils import KST, format_kst


class _MockSocket:
    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class TestLB063Concurrency(unittest.TestCase):
    """LB-063 동시성 회귀 테스트: DndSyncManager 직렬화·불변 outcome 및 FCM 락 해제 검증."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "lb063_conc.db"
        self.env_path = self.tmp_path / ".env"
        self.env_path.write_text("SMARTTHINGS_TOKEN=tok-conc\n", encoding="utf-8")
        self.st_log = self.tmp_path / "st.log"
        self.sched_log = self.tmp_path / "sched.log"
        self.base_time = datetime(2026, 11, 19, 13, 0, 0, tzinfo=KST)

    def tearDown(self) -> None:
        for name, path in (("server.scheduler", self.sched_log), ("server.smartthings", self.st_log)):
            logger = logging.getLogger(name)
            for h in list(logger.handlers):
                if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == path.resolve():
                    h.flush()
                    h.close()
                    logger.removeHandler(h)
        self.tmp.cleanup()

    def _make_client(self) -> SmartThingsClient:
        mgr = SmartThingsTokenManager.from_env(env_path=self.env_path, log_file=self.st_log)
        return SmartThingsClient(token_manager=mgr, simulated=True)

    def test_concurrent_dnd_sync_serializes_calls_and_isolates_per_call_outcome(self) -> None:
        """HTTP 요청과 스케줄러가 동시에 DndSyncManager에 접근해도 중복 호출 없이 각 호출 결과를 격리한다."""
        storage = Storage(self.db_path)
        client = self._make_client()
        dnd_m = DndSyncManager(storage=storage, client=client, device_id=DEFAULT_DEVICE_ID)
        sched = ReservationScheduler(
            storage=storage, client=client, device_id=DEFAULT_DEVICE_ID,
            log_file=self.sched_log, dnd_sync_manager=dnd_m, now_func=lambda: self.base_time,
        )
        srv = MiniPCServer(
            Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret="sec-conc"),
            storage=storage, scheduler=sched,
        )
        try:
            # 1) 동시 진입 시 get_dnd_status / set_dnd_mode 각 1회만 호출(중복 백업·명령 방지)
            in_dnd = threading.Event()
            release_dnd = threading.Event()
            get_calls = 0
            set_calls = 0
            orig_get = client.get_dnd_status
            orig_set = client.set_dnd_mode

            def slow_get(dev_id: str) -> dict[str, str]:
                nonlocal get_calls
                get_calls += 1
                in_dnd.set()
                release_dnd.wait(timeout=2.0)
                return orig_get(dev_id)

            def counted_set(dev_id: str, s: str, e: str, m: str) -> dict[str, Any]:
                nonlocal set_calls
                set_calls += 1
                return orig_set(dev_id, s, e, m)

            storage.save_off_period(OffPeriod(
                id="op-pre",
                start_at=self.base_time - timedelta(minutes=10),
                end_at=self.base_time + timedelta(hours=2),
            ))
            results: list[dict[str, Any]] = []
            with patch.object(client, "get_dnd_status", side_effect=slow_get), patch.object(
                client, "set_dnd_mode", side_effect=counted_set
            ):
                svc = OffPeriodService(
                    storage, now_func=lambda: self.base_time, dnd_sync_manager=dnd_m,
                    smartthings_health_getter=sched.get_smartthings_health,
                    on_dnd_sync=lambda dm, now, exc, out=None: record_server_dnd_outcome(srv, dm, now, exc, out),
                )
                t_http = threading.Thread(
                    target=lambda: results.append(svc.create_off_period_response({
                        "id": "op-http",
                        "start_at": format_kst(self.base_time - timedelta(minutes=5)),
                        "end_at": format_kst(self.base_time + timedelta(hours=1)),
                    }))
                )
                t_http.start()
                self.assertTrue(in_dnd.wait(timeout=1.0))

                # HTTP가 get_dnd_status 락을 잡고 있는 동안 스케줄러도 sync_with_outcome 동시 시도
                t_sched = threading.Thread(target=lambda: dnd_m.sync_with_outcome(self.base_time))
                t_sched.start()
                time.sleep(0.05)
                release_dnd.set()
                t_http.join(timeout=2.0)
                t_sched.join(timeout=2.0)

            self.assertEqual(get_calls, 1)
            self.assertEqual(set_calls, 1)
            self.assertEqual(len(results), 1)
            self.assertNotIn("warning", results[0])

            # 2) 호출 1(401 실패) 직후 호출 2(성공)가 뒤따라도 호출 1의 불변 outcome과 warning이 유실되지 않는다
            storage.clear_dnd_backup()
            call_seq = 0
            t1_started = threading.Event()
            allow_t1_fail = threading.Event()

            def seq_get(dev_id: str) -> dict[str, str]:
                nonlocal call_seq
                call_seq += 1
                if call_seq == 1:
                    t1_started.set()
                    allow_t1_fail.wait(timeout=2.0)
                    raise SmartThingsAuthError("401 concurrent")
                return orig_get(dev_id)

            warn_res: list[dict[str, Any]] = []
            ok_outcomes: list[Any] = []
            with patch.object(client, "get_dnd_status", side_effect=seq_get):
                svc2 = OffPeriodService(
                    storage, now_func=lambda: self.base_time, dnd_sync_manager=dnd_m,
                    smartthings_health_getter=sched.get_smartthings_health,
                    on_dnd_sync=lambda dm, now, exc, out=None: record_server_dnd_outcome(srv, dm, now, exc, out),
                )
                t1 = threading.Thread(
                    target=lambda: warn_res.append(svc2.create_off_period_response({
                        "id": "op-fail-race",
                        "start_at": format_kst(self.base_time - timedelta(minutes=5)),
                        "end_at": format_kst(self.base_time + timedelta(hours=1)),
                    }))
                )
                t1.start()
                self.assertTrue(t1_started.wait(timeout=1.0))

                t2 = threading.Thread(target=lambda: ok_outcomes.append(dnd_m.sync_with_outcome(self.base_time)))
                t2.start()
                time.sleep(0.05)
                allow_t1_fail.set()
                t1.join(timeout=2.0)
                t2.join(timeout=2.0)

            self.assertEqual(len(warn_res), 1)
            self.assertIn("warning", warn_res[0])
            self.assertIn("인증 실패", warn_res[0]["warning"])
            self.assertEqual(len(ok_outcomes), 1)
            self.assertTrue(ok_outcomes[0].succeeded)
        finally:
            srv.close()

    def test_apply_dnd_sync_outcome_consumes_once_without_double_count_or_misclassification(self) -> None:
        """apply_dnd_sync_outcome을 중복 호출해도 failure_count가 이중 증가하거나 api_error로 오분류되지 않는다."""
        storage = Storage(self.db_path)
        client = self._make_client()
        dnd_m = DndSyncManager(storage=storage, client=client, device_id=DEFAULT_DEVICE_ID)
        backoff = AuthBackoffTracker(logging.getLogger("test.lb063.conc"))
        try:
            storage.save_off_period(OffPeriod(
                id="op-consume",
                start_at=self.base_time - timedelta(minutes=5),
                end_at=self.base_time + timedelta(hours=1),
            ))
            with patch.object(client, "get_dnd_status", side_effect=SmartThingsAuthError("401 once")):
                dnd_m.sync(self.base_time)

            self.assertTrue(apply_dnd_sync_outcome(backoff, dnd_m, self.base_time))
            self.assertFalse(apply_dnd_sync_outcome(backoff, dnd_m, self.base_time))
            self.assertEqual(backoff.failure_count, 1)
            health = backoff.to_health_dict()
            self.assertEqual(health["smartthings_auth_state"], "auth_failed")
        finally:
            storage.close()

    def test_health_not_blocked_while_fcm_send_in_flight_and_failed_send_rolls_back_slot(self) -> None:
        """FCM 푸시 전송이 지연되는 동안 /health와 to_health_dict가 블로킹되지 않고, 실패 시 슬롯이 복원된다."""
        storage = Storage(self.db_path)
        fcm = FCMClient(simulated=True)
        notify = NotificationService(storage, fcm)
        notify.register_device("galaxy-s21-plus", "tok-s21")

        fcm_started = threading.Event()
        fcm_release = threading.Event()
        send_attempts = 0

        def blocking_send(**kwargs: Any) -> Any:
            nonlocal send_attempts
            send_attempts += 1
            fcm_started.set()
            fcm_release.wait(timeout=3.0)
            if send_attempts == 1:
                raise RuntimeError("fcm temporary outage")
            return NotificationService.send_to_all(notify, **kwargs)

        sched = ReservationScheduler(
            storage=storage, client=self._make_client(), device_id=DEFAULT_DEVICE_ID,
            log_file=self.sched_log, notification_service=notify,
            auth_backoff_base=1.0, auth_alert_threshold=2, auth_alert_cooldown=3600.0,
        )
        srv = MiniPCServer(
            Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret="sec-fcm"),
            storage=storage, notification_service=notify, scheduler=sched,
        )
        try:
            sched.auth_backoff.record_failure(self.base_time, SmartThingsAuthError("401-1"))
            with patch.object(notify, "send_to_all", side_effect=blocking_send):
                t_fail = threading.Thread(
                    target=lambda: sched.auth_backoff.record_failure(
                        self.base_time + timedelta(seconds=1), SmartThingsAuthError("401-2")
                    )
                )
                t_fail.start()
                self.assertTrue(fcm_started.wait(timeout=1.0))

                # FCM 전송 중에도 to_health_dict() 와 GET /health 가 즉시 응답해야 함
                t0 = time.monotonic()
                sock = _MockSocket(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                MiniPCHandler(sock, ("127.0.0.1", 12345), srv)  # type: ignore[arg-type]
                elapsed = time.monotonic() - t0
                body = json.loads(sock.wfile.getvalue().decode("utf-8").split("\r\n\r\n", 1)[1])
                self.assertLess(elapsed, 0.3)
                self.assertIs(body["smartthings_ok"], False)
                self.assertEqual(body["smartthings_auth_state"], "auth_failed")

                # 전송 중 동시 실패가 발생해도 중복 발송되지 않음
                sched.auth_backoff.record_failure(self.base_time + timedelta(seconds=2), SmartThingsAuthError("401-3"))
                self.assertEqual(send_attempts, 1)

                fcm_release.set()
                t_fail.join(timeout=2.0)

            # 1차 FCM 예외 발생으로 쿨다운 슬롯이 복원되어, 다음 실패에서 재시도 성공
            sched.auth_backoff.record_failure(self.base_time + timedelta(seconds=5), SmartThingsAuthError("401-4"))
            self.assertEqual(send_attempts, 1)
            self.assertEqual(len(fcm.dispatched_messages), 1)
        finally:
            fcm_release.set()
            srv.close()


if __name__ == "__main__":
    unittest.main()
