from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import STATUS_SCHEDULED
from server.server import MiniPCHandler, MiniPCServer, create_server
from server.time_utils import format_kst, now_kst


class MockSocket:
    """테스트용 인메모리 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        if "r" in mode:
            return self.rfile
        return self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class TestServer(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "server_test.db"
        self.test_secret = "test-secret-key-1234"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _request(
        self,
        server: MiniPCServer,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        secret: str | None = "test-secret-key-1234",
    ) -> tuple[int, dict[str, Any]]:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
        req_headers = dict(headers or {})
        if payload is not None and "Content-Type" not in req_headers:
            req_headers["Content-Type"] = "application/json"
        if payload is not None and "Content-Length" not in req_headers:
            req_headers["Content-Length"] = str(len(body_bytes))
        if secret is not None and "X-Shared-Secret" not in req_headers and "Authorization" not in req_headers:
            req_headers["X-Shared-Secret"] = secret
        for k, v in req_headers.items():
            lines.append(f"{k}: {v}")
        raw_req = "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body_bytes
        sock = MockSocket(raw_req)
        MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]

        raw_resp = sock.wfile.getvalue().decode("utf-8")
        status_line, rest = raw_resp.split("\r\n", 1)
        status_code = int(status_line.split(" ")[1])
        parts = rest.split("\r\n\r\n", 1)
        body_text = parts[1] if len(parts) > 1 else ""
        data = json.loads(body_text) if body_text else {}
        return status_code, data

    def test_health_check_endpoints(self) -> None:
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path))
        try:
            for path in ("/", "/health", "/healthz", "/api/health"):
                status, body = self._request(srv, "GET", path)
                self.assertEqual(status, 200, f"Failed for path {path}")
                self.assertEqual(body.get("status"), "ok")
                self.assertIn("time_kst", body)
                self.assertIn("+09:00", body["time_kst"])
        finally:
            srv.close()

    def test_persistence_across_server_restart(self) -> None:
        """done_when: 서버를 실행하면 상태 확인 주소가 응답하고,

        저장한 시험 데이터가 서버 프로세스를 껐다 켠 뒤에도 그대로 조회된다.
        """
        # --- 1단계: 첫 번째 서버 인스턴스 실행 및 데이터 저장 ---
        srv1 = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.test_secret))
        try:
            # 1. 상태 확인 주소 응답 확인
            st_health, body_health = self._request(srv1, "GET", "/health")
            self.assertEqual(st_health, 200)
            self.assertEqual(body_health["status"], "ok")

            # 2. Reservation 시험 데이터 저장
            res_payload = {
                "id": "res-test-1",
                "reserved_at": format_kst((now_kst() + timedelta(hours=3)).replace(second=0, microsecond=0)),
                "status": STATUS_SCHEDULED,
                "allow_during_off_period": True,
            }
            st_res, body_res = self._request(srv1, "POST", "/api/reservations", res_payload)
            self.assertEqual(st_res, 201)
            self.assertEqual(body_res["id"], "res-test-1")

            # 3. OffPeriod 시험 데이터 저장
            op_payload = {
                "id": "op-test-1",
                "start_at": format_kst(now_kst()),
                "end_at": format_kst(now_kst() + timedelta(days=2)),
            }
            st_op, body_op = self._request(srv1, "POST", "/api/off-periods", op_payload)
            self.assertEqual(st_op, 201)
            self.assertEqual(body_op["id"], "op-test-1")

            # 4. AutoStopEvent 시험 데이터 저장
            evt_payload = {
                "id": "evt-test-1",
                "occurred_at": format_kst(now_kst()),
                "off_period_id": "op-test-1",
                "success": True,
                "details": "테스트 자동 정지",
            }
            st_evt, _ = self._request(srv1, "POST", "/api/auto-stop-events", evt_payload)
            self.assertEqual(st_evt, 201)

            # 5. WaterTracker 시험 데이터 저장
            wt_payload = {
                "mop_clean_count": 4,
                "alert_threshold_n": 6,
            }
            st_wt, body_wt = self._request(srv1, "POST", "/api/water-tracker", wt_payload)
            self.assertEqual(st_wt, 200)
            self.assertEqual(body_wt["mop_clean_count"], 4)

            # 6. NotificationTarget 시험 데이터 저장 (두 대)
            nt1_payload = {"id": "nt-s21", "device_id": "galaxy-s21-plus", "push_token": "token-user1"}
            nt2_payload = {"id": "nt-fold8", "device_id": "galaxy-fold-8", "push_token": "token-user2"}
            st_nt1, _ = self._request(srv1, "POST", "/api/notification-targets", nt1_payload)
            st_nt2, _ = self._request(srv1, "POST", "/api/notification-targets", nt2_payload)
            self.assertEqual(st_nt1, 201)
            self.assertEqual(st_nt2, 201)
        finally:
            # 첫 번째 서버 인스턴스 완전히 종료 (프로세스 끔)
            srv1.close()

        # --- 2단계: 동일한 DB 파일로 두 번째 서버 재실행 (프로세스 켬) ---
        srv2 = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.test_secret))
        try:
            # 상태 확인 주소 정상 응답
            st_health2, body_health2 = self._request(srv2, "GET", "/health")
            self.assertEqual(st_health2, 200)
            self.assertEqual(body_health2["status"], "ok")

            # 껐다 켠 뒤에도 저장했던 시험 데이터가 그대로 조회되는지 검증
            # 1) 예약 조회
            st_res_get, body_res_get = self._request(srv2, "GET", "/api/reservations/res-test-1")
            self.assertEqual(st_res_get, 200)
            self.assertEqual(body_res_get["id"], "res-test-1")
            self.assertEqual(body_res_get["status"], STATUS_SCHEDULED)
            self.assertTrue(body_res_get["allow_during_off_period"])

            # 2) 쉬는 기간 조회
            st_op_get, body_op_get = self._request(srv2, "GET", "/api/off-periods/op-test-1")
            self.assertEqual(st_op_get, 200)
            self.assertEqual(body_op_get["id"], "op-test-1")

            # 3) 자동 정지 기록 조회
            st_evts, body_evts = self._request(srv2, "GET", "/api/auto-stop-events")
            self.assertEqual(st_evts, 200)
            self.assertEqual(len(body_evts["auto_stop_events"]), 1)
            self.assertEqual(body_evts["auto_stop_events"][0]["id"], "evt-test-1")

            # 4) 물 관리 기록 조회
            st_wt_get, body_wt_get = self._request(srv2, "GET", "/api/water-tracker")
            self.assertEqual(st_wt_get, 200)
            self.assertEqual(body_wt_get["mop_clean_count"], 4)
            self.assertEqual(body_wt_get["alert_threshold_n"], 6)

            # 5) 푸시 대상 목록 조회 (두 대 모두 유지)
            st_nt_get, body_nt_get = self._request(srv2, "GET", "/api/notification-targets")
            self.assertEqual(st_nt_get, 200)
            targets = body_nt_get["notification_targets"]
            self.assertEqual(len(targets), 2)
            device_ids = {t["device_id"] for t in targets}
            self.assertIn("galaxy-s21-plus", device_ids)
            self.assertIn("galaxy-fold-8", device_ids)
        finally:
            srv2.close()

    def test_error_and_delete_handling(self) -> None:
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.test_secret))
        try:
            # 404 Unknown path
            st, _ = self._request(srv, "GET", "/nonexistent")
            self.assertEqual(st, 404)

            # 404 Delete non-existent
            st_del, _ = self._request(srv, "DELETE", "/api/reservations/not-exists")
            self.assertEqual(st_del, 404)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
