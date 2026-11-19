from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from server.config import Config
from server.notify import FCMClient, NotificationService
from server.off_periods.auto_stop import DEFAULT_DEVICE_ID
from server.off_periods.dnd_sync import DndSyncManager
from server.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer, create_server
from server.smartthings.auth import SmartThingsTokenManager
from server.smartthings.client import SmartThingsApiError, SmartThingsAuthError, SmartThingsClient
from server.storage import Storage
from server.time_utils import KST, format_kst, now_kst


def _close_log(logger_name: str, path: Path) -> None:
    logger = logging.getLogger(logger_name)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == path.resolve():
            h.flush()
            h.close()
            logger.removeHandler(h)


class MockSocket:
    """테스트용 인메모리 HTTP 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class TestLB063SmartThingsHealthAndAlerts(unittest.TestCase):
    """LB-063: /health 스마트싱스 상태 노출, 쉬는 기간 DND 실패 경고, 연속 인증 실패 푸시 쿨다운."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "lb063.db"
        self.sched_log = self.tmp_path / "sched.log"
        self.st_log = self.tmp_path / "st.log"
        self.env_path = self.tmp_path / ".env"
        self.token_file = self.tmp_path / "st_token.json"
        self.secret = "lb063-secret-1234"
        self.base_time = datetime(2026, 11, 19, 12, 0, 0, tzinfo=KST)

    def tearDown(self) -> None:
        _close_log("server.scheduler", self.sched_log)
        _close_log("server.smartthings", self.st_log)
        self.tmp.cleanup()

    def _request(
        self, server: MiniPCServer, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost", f"X-Shared-Secret: {self.secret}"]
        if payload is not None:
            lines.extend(["Content-Type: application/json", f"Content-Length: {len(body_bytes)}"])
        sock = MockSocket("\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body_bytes)
        MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
        raw = sock.wfile.getvalue().decode("utf-8")
        status_line, rest = raw.split("\r\n", 1)
        body = rest.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in rest else ""
        return int(status_line.split(" ")[1]), (json.loads(body) if body else {})

    def _build_stack(
        self, simulated: bool = False, **sched_kwargs: Any
    ) -> tuple[Storage, SmartThingsClient, FCMClient, NotificationService, ReservationScheduler, MiniPCServer]:
        self.env_path.write_text("SMARTTHINGS_TOKEN=tok-init\n", encoding="utf-8")
        mgr = SmartThingsTokenManager.from_env(
            env_path=self.env_path, log_file=self.st_log, token_file=self.token_file
        )
        client = SmartThingsClient(token_manager=mgr, simulated=simulated)
        storage = Storage(self.db_path)
        fcm = FCMClient(simulated=True)
        notify = NotificationService(storage, fcm)
        dnd_m = DndSyncManager(storage=storage, client=client, device_id=DEFAULT_DEVICE_ID)
        sched = ReservationScheduler(
            storage=storage, client=client, device_id=DEFAULT_DEVICE_ID,
            log_file=self.sched_log, dnd_sync_manager=dnd_m, **sched_kwargs,
        )
        srv = MiniPCServer(
            Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret),
            storage=storage, notification_service=notify, scheduler=sched,
        )
        return storage, client, fcm, notify, sched, srv

    def test_health_exposes_smartthings_fields_and_preserves_legacy_fields(self) -> None:
        """GET /health 는 기존 필드를 유지하면서 smartthings_ok/auth_state/last_success_at/last_error 를 노출한다."""
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret))
        try:
            for path in ("/", "/health", "/healthz", "/api/health"):
                code, body = self._request(srv, "GET", path)
                self.assertEqual(code, 200)
                self.assertEqual(body["status"], "ok")
                self.assertEqual(body["service"], "smart-things-mini-pc")
                self.assertIn("+09:00", body["time_kst"])
                self.assertEqual(body["database"], "connected")
                self.assertIs(body["smartthings_ok"], True)
                self.assertEqual(body["smartthings_auth_state"], "unknown")
                self.assertIsNone(body["last_success_at"])
                self.assertIsNone(body["last_error"])
        finally:
            srv.close()

    def test_health_tracks_success_auth_failure_api_error_and_recovery(self) -> None:
        """스케줄러의 SmartThings 성공, 401 인증 실패, API 오류, 복구가 /health 에 반영된다."""
        _, client, _, _, sched, srv = self._build_stack(simulated=False, auth_backoff_base=10.0)
        try:
            with patch.object(client, "get_device_status", return_value=object()):
                sched.step(self.base_time)
            code, h_ok = self._request(srv, "GET", "/health")
            self.assertEqual(code, 200)
            self.assertIs(h_ok["smartthings_ok"], True)
            self.assertEqual(h_ok["smartthings_auth_state"], "ok")
            self.assertEqual(h_ok["last_success_at"], format_kst(self.base_time))
            self.assertIsNone(h_ok["last_error"])

            with patch.object(client, "get_device_status", side_effect=SmartThingsApiError("timeout-503")):
                sched.step(self.base_time + timedelta(seconds=5))
            _, h_api = self._request(srv, "GET", "/health")
            self.assertIs(h_api["smartthings_ok"], False)
            self.assertEqual(h_api["smartthings_auth_state"], "api_error")
            self.assertEqual(h_api["last_success_at"], format_kst(self.base_time))
            self.assertIn("timeout-503", h_api["last_error"])
            self.assertEqual(sched.auth_backoff.failure_count, 0)

            with patch.object(client, "get_device_status", side_effect=SmartThingsAuthError("HTTP 401 invalid")):
                sched.step(self.base_time + timedelta(seconds=10))
            _, h_auth = self._request(srv, "GET", "/health")
            self.assertIs(h_auth["smartthings_ok"], False)
            self.assertEqual(h_auth["smartthings_auth_state"], "auth_failed")
            self.assertEqual(h_auth["last_success_at"], format_kst(self.base_time))
            self.assertIn("HTTP 401 invalid", h_auth["last_error"])

            self.env_path.write_text("SMARTTHINGS_TOKEN=tok-refreshed\n", encoding="utf-8")
            t_rec = self.base_time + timedelta(seconds=12)
            with patch.object(client, "get_device_status", return_value=object()):
                sched.step(t_rec)
            _, h_rec = self._request(srv, "GET", "/health")
            self.assertIs(h_rec["smartthings_ok"], True)
            self.assertEqual(h_rec["smartthings_auth_state"], "ok")
            self.assertEqual(h_rec["last_success_at"], format_kst(t_rec))
            self.assertIsNone(h_rec["last_error"])
        finally:
            srv.close()

    def test_post_off_period_returns_warning_on_dnd_failure_and_auth_failure(self) -> None:
        """POST /api/off-periods 호출 시 DND 동기화 실패 또는 인증 실패 상태면 warning 필드를 반환한다."""
        storage, client, _, _, sched, srv = self._build_stack(simulated=True)
        try:
            now = now_kst().replace(second=0, microsecond=0)
            active = {
                "start_at": format_kst(now - timedelta(minutes=10)),
                "end_at": format_kst(now + timedelta(hours=2)),
            }

            code, body_ok = self._request(srv, "POST", "/api/off-periods", {"id": "op-ok", **active})
            self.assertEqual(code, 201)
            self.assertNotIn("warning", body_ok)
            self._request(srv, "DELETE", "/api/off-periods/op-ok")

            with patch.object(client, "get_dnd_status", side_effect=SmartThingsApiError("dnd api down")):
                code_api, body_api = self._request(srv, "POST", "/api/off-periods", {"id": "op-api-err", **active})
            self.assertEqual(code_api, 201)
            self.assertIsNotNone(storage.get_off_period("op-api-err"))
            self.assertIn("dnd api down", body_api["warning"])
            _, h_api = self._request(srv, "GET", "/health")
            self.assertIs(h_api["smartthings_ok"], False)
            self.assertEqual(h_api["smartthings_auth_state"], "api_error")

            with patch.object(client, "get_dnd_status", side_effect=SmartThingsAuthError("401 expired")):
                code_auth, body_auth = self._request(srv, "POST", "/api/off-periods", {"id": "op-auth-err", **active})
            self.assertEqual(code_auth, 201)
            self.assertIsNotNone(storage.get_off_period("op-auth-err"))
            self.assertIn("인증 실패", body_auth["warning"])
            _, h_auth = self._request(srv, "GET", "/health")
            self.assertEqual(h_auth["smartthings_auth_state"], "auth_failed")

            self._request(srv, "DELETE", "/api/off-periods/op-auth-err")
            self._request(srv, "DELETE", "/api/off-periods/op-api-err")
            sched.auth_backoff.record_failure(now, SmartThingsAuthError("token still expired"))
            future = {
                "id": "op-future",
                "start_at": format_kst(now + timedelta(days=5)),
                "end_at": format_kst(now + timedelta(days=6)),
            }
            code_fut, body_fut = self._request(srv, "POST", "/api/off-periods", future)
            self.assertEqual(code_fut, 201)
            self.assertIsNotNone(storage.get_off_period("op-future"))
            self.assertIn("인증", body_fut["warning"])

            code_rec, body_rec = self._request(srv, "POST", "/api/off-periods", {"id": "op-rec", **active})
            self.assertEqual(code_rec, 201)
            self.assertNotIn("warning", body_rec)
            _, h_rec = self._request(srv, "GET", "/health")
            self.assertIs(h_rec["smartthings_ok"], True)
        finally:
            srv.close()

    def test_consecutive_auth_failures_send_push_with_cooldown(self) -> None:
        """연속 인증 실패 임계치(3회) 도달 시 모든 등록 기기에 푸시가 발송되고 쿨다운 동안 중복 발송되지 않는다."""
        _, _, fcm, notify, sched, srv = self._build_stack(
            simulated=False, auth_backoff_base=10.0, auth_backoff_max=60.0,
            auth_alert_threshold=3, auth_alert_cooldown=3600.0,
        )
        notify.register_device("galaxy-s21-plus", "tok-s21")
        notify.register_device("galaxy-fold-8", "tok-fold8")
        try:
            http_401 = urllib.error.HTTPError(
                "https://api.smartthings.com/v1/devices/x/status", 401, "Unauthorized", {}, io.BytesIO(b"{}")  # type: ignore[arg-type]
            )
            with patch("urllib.request.urlopen", side_effect=http_401):
                sched.step(self.base_time)
                sched.step(self.base_time + timedelta(seconds=10))
                self.assertEqual(sched.auth_backoff.failure_count, 2)
                self.assertEqual(len(fcm.dispatched_messages), 0)

                sched.step(self.base_time + timedelta(seconds=30))
                self.assertEqual(sched.auth_backoff.failure_count, 3)
                self.assertEqual(len(fcm.dispatched_messages), 2)
                devs = {m["device_id"] for m in fcm.dispatched_messages}
                self.assertEqual(devs, {"galaxy-s21-plus", "galaxy-fold-8"})
                self.assertEqual(fcm.dispatched_messages[0]["data"]["type"], "smartthings_auth_failed")

                sched.step(self.base_time + timedelta(seconds=70))
                sched.step(self.base_time + timedelta(seconds=130))
                sched.step(self.base_time + timedelta(seconds=3600))
                self.assertEqual(sched.auth_backoff.failure_count, 6)
                self.assertEqual(len(fcm.dispatched_messages), 2)

                sched.step(self.base_time + timedelta(seconds=3660))
                self.assertEqual(sched.auth_backoff.failure_count, 7)
                self.assertEqual(len(fcm.dispatched_messages), 4)
        finally:
            srv.close()

    def test_transient_failures_below_threshold_do_not_alert_after_recovery(self) -> None:
        """임계치 미만 실패 후 인증이 복구되면 실패 횟수가 초기화되어 알림이 발송되지 않는다."""
        _, client, fcm, notify, sched, srv = self._build_stack(
            simulated=True, auth_backoff_base=5.0, auth_alert_threshold=3,
        )
        notify.register_device("galaxy-s21-plus", "tok-s21")
        try:
            with patch.object(client, "get_device_status", side_effect=SmartThingsAuthError("401")):
                sched.step(self.base_time)
                sched.step(self.base_time + timedelta(seconds=5))
            self.assertEqual(sched.auth_backoff.failure_count, 2)
            self.assertEqual(len(fcm.dispatched_messages), 0)

            with patch.object(client, "get_device_status", return_value=object()):
                sched.step(self.base_time + timedelta(seconds=15))
            self.assertEqual(sched.auth_backoff.failure_count, 0)

            with patch.object(client, "get_device_status", side_effect=SmartThingsAuthError("401")):
                sched.step(self.base_time + timedelta(seconds=20))
            self.assertEqual(sched.auth_backoff.failure_count, 1)
            self.assertEqual(len(fcm.dispatched_messages), 0)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
