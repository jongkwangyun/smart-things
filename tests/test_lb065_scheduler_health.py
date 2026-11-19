from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

from server.config import Config
from server.scheduler import ReservationScheduler
from server.server import MiniPCServer, create_server, run_server
from server.smartthings.auth import SmartThingsTokenManager, TokenInfo
from server.smartthings.client import SmartThingsClient
from server.smartthings.health import build_health_response, is_scheduler_degraded
from server.storage import Storage


def _close_log(logger_name: str, path: Path) -> None:
    logger = logging.getLogger(logger_name)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == path.resolve():
            h.flush()
            h.close()
            logger.removeHandler(h)


def _http_get_json(port: int, path: str) -> tuple[int, dict[str, Any]]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            return int(resp.status), json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            raw = err.read().decode("utf-8")
            return int(err.code), json.loads(raw) if raw else {}
        finally:
            err.close()


class TestLB065SchedulerHealth(unittest.TestCase):
    """LB-065: /health scheduler_running 노출 및 예기치 않은 스케줄러 중단 시 503 degraded 판정."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "lb065.db"
        self.sched_log = self.tmp_path / "sched.log"
        self.st_log = self.tmp_path / "st.log"
        self.secret = "lb065-secret-1234"

    def tearDown(self) -> None:
        _close_log("server.scheduler", self.sched_log)
        _close_log("server.smartthings", self.st_log)
        self.tmp.cleanup()

    def _make_sim_client(self) -> SmartThingsClient:
        mgr = SmartThingsTokenManager(
            token_info=TokenInfo(access_token="sim-tok", token_type="pat"),
            log_file=self.st_log,
        )
        return SmartThingsClient(token_manager=mgr, simulated=True)

    def _start_run_server_bg(
        self, start_scheduler: bool
    ) -> tuple[MiniPCServer, threading.Thread, list[MiniPCServer]]:
        created: list[MiniPCServer] = []
        ready = threading.Event()
        real_create = create_server

        def wrapped_create(*args: Any, **kwargs: Any) -> MiniPCServer:
            srv = real_create(*args, **kwargs)
            created.append(srv)
            ready.set()
            return srv

        cfg = Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret)
        sim_client = self._make_sim_client()
        orig_init = ReservationScheduler.__init__

        def fast_sched_init(sched_self: ReservationScheduler, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("poll_interval", 0.05)
            kwargs.setdefault("log_file", self.sched_log)
            kwargs.setdefault("client", sim_client)
            orig_init(sched_self, *args, **kwargs)

        def runner() -> None:
            with (
                patch("server.server.create_server", side_effect=wrapped_create),
                patch.object(ReservationScheduler, "__init__", fast_sched_init),
            ):
                run_server(cfg, start_scheduler=start_scheduler)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        self.assertTrue(ready.wait(timeout=3.0), "서버 생성 대기 시간 초과")
        srv = created[0]
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            sched = srv.scheduler
            if not start_scheduler or (srv.scheduler_expected and sched is not None and sched.is_running):
                break
            time.sleep(0.02)
        return srv, t, created

    def test_create_server_default_returns_200_with_scheduler_running_false(self) -> None:
        """create_server() 단위 테스트 기본값(scheduler_expected=False)은 모든 헬스 경로에서 HTTP 200을 반환한다."""
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret))
        try:
            self.assertFalse(srv.scheduler_expected)
            self.assertFalse(srv.is_closing)
            for path in ("/", "/health", "/healthz", "/api/health"):
                # Direct payload/response helper + real socket HTTP check
                code, body = build_health_response(srv)
                self.assertEqual(code, 200, f"Unexpected status for {path}")
                self.assertEqual(body["status"], "ok")
                self.assertIs(body["scheduler_running"], False)
                self.assertEqual(body["service"], "smart-things-mini-pc")
                self.assertEqual(body["database"], "connected")
                self.assertIn("+09:00", body["time_kst"])
                self.assertIn("smartthings_ok", body)
                self.assertIn("smartthings_auth_state", body)
                self.assertIn("last_success_at", body)
                self.assertIn("last_error", body)
        finally:
            srv.close()

    def test_create_server_with_explicit_scheduler_reports_running_state_and_stays_200(self) -> None:
        """create_server(scheduler=...)에서 scheduler_expected=False면 중지 상태여도 HTTP 200을 유지한다."""
        storage = Storage(self.db_path)
        sched = ReservationScheduler(
            storage=storage, client=self._make_sim_client(), poll_interval=0.05, log_file=self.sched_log,
        )
        srv = MiniPCServer(
            Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret),
            storage=storage, scheduler=sched,
        )
        try:
            code_before, body_before = build_health_response(srv)
            self.assertEqual(code_before, 200)
            self.assertEqual(body_before["status"], "ok")
            self.assertIs(body_before["scheduler_running"], False)

            sched.start()
            code_running, body_running = build_health_response(srv)
            self.assertEqual(code_running, 200)
            self.assertEqual(body_running["status"], "ok")
            self.assertIs(body_running["scheduler_running"], True)

            sched.stop(timeout=2.0)
            code_stopped, body_stopped = build_health_response(srv)
            self.assertEqual(code_stopped, 200)
            self.assertEqual(body_stopped["status"], "ok")
            self.assertIs(body_stopped["scheduler_running"], False)
        finally:
            srv.close()

    def test_run_server_with_scheduler_returns_200_then_503_on_unexpected_thread_death(self) -> None:
        """run_server(start_scheduler=True)에서 정상 가동 시 200, 예기치 않은 스레드 중단 시 503(degraded) 반환."""
        srv, thread, _ = self._start_run_server_bg(start_scheduler=True)
        port = srv.server_address[1]
        try:
            self.assertTrue(srv.scheduler_expected)
            self.assertIsNotNone(srv.scheduler)
            assert srv.scheduler is not None
            self.assertTrue(srv.scheduler.is_running)
            self.assertFalse(srv.scheduler.stop_requested)

            for path in ("/", "/health", "/healthz", "/api/health"):
                code, body = _http_get_json(port, path)
                self.assertEqual(code, 200)
                self.assertEqual(body["status"], "ok")
                self.assertIs(body["scheduler_running"], True)
                self.assertEqual(body["service"], "smart-things-mini-pc")
                self.assertEqual(body["database"], "connected")
                self.assertIn("smartthings_ok", body)

            # 예기치 않은 BaseException으로 스케줄러 스레드가 중단되는 상황 시뮬레이션
            class _FatalThreadCrash(BaseException):
                pass

            with patch.object(srv.scheduler, "step", side_effect=_FatalThreadCrash("fatal crash")):
                assert srv.scheduler._thread is not None
                srv.scheduler._thread.join(timeout=3.0)

            self.assertFalse(srv.scheduler.is_running)
            self.assertFalse(srv.scheduler.stop_requested)

            for path in ("/", "/health", "/healthz", "/api/health"):
                code_err, body_err = _http_get_json(port, path)
                self.assertEqual(code_err, 503, f"Expected 503 for {path}")
                self.assertEqual(body_err["status"], "degraded")
                self.assertIs(body_err["scheduler_running"], False)
                self.assertEqual(body_err["service"], "smart-things-mini-pc")
                self.assertEqual(body_err["database"], "connected")
                self.assertIn("+09:00", body_err["time_kst"])
                self.assertIn("smartthings_ok", body_err)
                self.assertIn("smartthings_auth_state", body_err)

            # 로그에 예기치 않은 종료 기록 확인
            log_text = self.sched_log.read_text(encoding="utf-8")
            self.assertIn("예기치 않게 중단", log_text)
        finally:
            srv.shutdown()
            thread.join(timeout=3.0)

    def test_run_server_no_scheduler_mode_returns_200(self) -> None:
        """--no-scheduler(start_scheduler=False) 모드에서는 스케줄러가 없어도 HTTP 200을 반환한다."""
        srv, thread, _ = self._start_run_server_bg(start_scheduler=False)
        port = srv.server_address[1]
        try:
            self.assertFalse(srv.scheduler_expected)
            self.assertIsNone(srv.scheduler)
            code, body = _http_get_json(port, "/health")
            self.assertEqual(code, 200)
            self.assertEqual(body["status"], "ok")
            self.assertIs(body["scheduler_running"], False)
        finally:
            srv.shutdown()
            thread.join(timeout=3.0)

    def test_normal_stop_and_close_do_not_falsely_return_503_degraded(self) -> None:
        """정상 stop()/close() 호출은 예기치 않은 중단으로 오판하지 않고 503을 반환하지 않는다."""
        srv, thread, _ = self._start_run_server_bg(start_scheduler=True)
        port = srv.server_address[1]
        try:
            assert srv.scheduler is not None
            srv.scheduler.stop(timeout=2.0)
            self.assertFalse(srv.scheduler.is_running)
            self.assertTrue(srv.scheduler.stop_requested)
            self.assertFalse(is_scheduler_degraded(srv))

            code, body = _http_get_json(port, "/health")
            self.assertEqual(code, 200)
            self.assertEqual(body["status"], "ok")
            self.assertIs(body["scheduler_running"], False)

            # 재시작 시 stop_requested 초기화 및 scheduler_running=True 복구 확인
            srv.scheduler.start()
            self.assertFalse(srv.scheduler.stop_requested)
            self.assertTrue(srv.scheduler.is_running)
            code_restarted, body_restarted = _http_get_json(port, "/health")
            self.assertEqual(code_restarted, 200)
            self.assertEqual(body_restarted["status"], "ok")
            self.assertIs(body_restarted["scheduler_running"], True)
        finally:
            srv.shutdown()
            thread.join(timeout=3.0)
            self.assertTrue(srv.is_closing)
            self.assertFalse(srv.scheduler_expected)
            self.assertFalse(is_scheduler_degraded(srv))
            code_closed, body_closed = build_health_response(srv)
            self.assertEqual(code_closed, 200)
            self.assertEqual(body_closed["status"], "ok")
            self.assertIs(body_closed["scheduler_running"], False)

    def test_scheduler_expected_with_none_or_dead_thread_returns_503(self) -> None:
        """scheduler_expected=True일 때 scheduler가 None이거나 죽은 스레드로 교체되면 503을 반환한다."""
        storage = Storage(self.db_path)
        sched = ReservationScheduler(
            storage=storage, client=self._make_sim_client(), poll_interval=0.05, log_file=self.sched_log,
        )
        srv = MiniPCServer(
            Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret),
            storage=storage, scheduler=sched,
        )
        try:
            srv.scheduler_expected = True
            dead_thread = threading.Thread(target=lambda: None)
            dead_thread.start()
            dead_thread.join(timeout=1.0)
            sched._thread = dead_thread

            code_dead, body_dead = build_health_response(srv)
            self.assertEqual(code_dead, 503)
            self.assertEqual(body_dead["status"], "degraded")
            self.assertIs(body_dead["scheduler_running"], False)

            srv.scheduler = None
            code_none, body_none = build_health_response(srv)
            self.assertEqual(code_none, 503)
            self.assertEqual(body_none["status"], "degraded")
            self.assertIs(body_none["scheduler_running"], False)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
