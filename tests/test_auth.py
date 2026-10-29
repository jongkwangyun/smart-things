from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from server.auth import (
    extract_secret_from_headers,
    is_health_check_path,
    verify_shared_secret,
)
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


class TestAuth(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "auth_test.db"
        self.valid_secret = "correct-shared-secret-key-1234"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _request(
        self,
        server: MiniPCServer,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
        req_headers = dict(headers or {})
        if payload is not None and "Content-Type" not in req_headers:
            req_headers["Content-Type"] = "application/json"
        if payload is not None and "Content-Length" not in req_headers:
            req_headers["Content-Length"] = str(len(body_bytes))
        for k, v in req_headers.items():
            lines.append(f"{k}: {v}")
        raw_req = "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body_bytes
        sock = MockSocket(raw_req)
        MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]

        raw_resp = sock.wfile.getvalue().decode("utf-8")
        status_line, rest = raw_resp.split("\r\n", 1)
        status_code = int(status_line.split(" ")[1])
        parts = rest.split("\r\n\r\n", 1)
        header_lines = parts[0].split("\r\n")
        resp_headers: dict[str, str] = {}
        for hline in header_lines:
            if ": " in hline:
                hk, hv = hline.split(": ", 1)
                resp_headers[hk.lower()] = hv
        body_text = parts[1] if len(parts) > 1 else ""
        data = json.loads(body_text) if body_text else {}
        return status_code, data, resp_headers

    def test_no_secret_returns_401_and_no_data(self) -> None:
        """done_when: 비밀키 없이 예약 목록 주소를 요청하면 거부 응답만 오고 데이터가 보이지 않는다."""
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.valid_secret))
        try:
            # 먼저 올바른 키로 시험 예약 1건 저장
            seed_payload = {
                "id": "res-secret-test",
                "reserved_at": format_kst((now_kst() + timedelta(hours=1)).replace(second=0, microsecond=0)),
                "status": STATUS_SCHEDULED,
            }
            st_post, _, _ = self._request(
                srv, "POST", "/api/reservations", seed_payload, headers={"X-Shared-Secret": self.valid_secret}
            )
            self.assertEqual(st_post, 201)

            # 1. 헤더 없이 요청
            status, body, headers = self._request(srv, "GET", "/api/reservations")
            self.assertEqual(status, 401)
            self.assertIn("error", body)
            self.assertEqual(body.get("status"), 401)
            self.assertNotIn("reservations", body)
            self.assertIn("www-authenticate", headers)
            # 데이터가 전혀 포함되지 않았음을 확인
            self.assertNotIn("res-secret-test", json.dumps(body))
        finally:
            srv.close()

    def test_invalid_secret_returns_401_and_no_data(self) -> None:
        """done_when: 틀린 키로 예약 목록 주소를 요청하면 거부 응답만 오고 데이터가 보이지 않는다."""
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.valid_secret))
        try:
            # 1. X-Shared-Secret 틀린 키
            status, body, _ = self._request(
                srv, "GET", "/api/reservations", headers={"X-Shared-Secret": "wrong-secret-key"}
            )
            self.assertEqual(status, 401)
            self.assertIn("error", body)
            self.assertNotIn("reservations", body)

            # 2. Authorization: Bearer 틀린 키
            status_b, body_b, _ = self._request(
                srv, "GET", "/api/reservations", headers={"Authorization": "Bearer wrong-secret-token"}
            )
            self.assertEqual(status_b, 401)
            self.assertIn("error", body_b)
            self.assertNotIn("reservations", body_b)
        finally:
            srv.close()

    def test_valid_secret_returns_200_and_data(self) -> None:
        """done_when: 올바른 키로는 목록이 보인다."""
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.valid_secret))
        try:
            # 예약 1건 저장
            seed_payload = {
                "id": "res-view-1",
                "reserved_at": format_kst((now_kst() + timedelta(hours=2)).replace(second=0, microsecond=0)),
                "status": STATUS_SCHEDULED,
            }
            self._request(
                srv, "POST", "/api/reservations", seed_payload, headers={"X-Shared-Secret": self.valid_secret}
            )

            # 1. X-Shared-Secret 로 조회
            st1, body1, _ = self._request(
                srv, "GET", "/api/reservations", headers={"X-Shared-Secret": self.valid_secret}
            )
            self.assertEqual(st1, 200)
            self.assertIn("reservations", body1)
            self.assertEqual(len(body1["reservations"]), 1)
            self.assertEqual(body1["reservations"][0]["id"], "res-view-1")

            # 2. Authorization: Bearer 로 조회
            st2, body2, _ = self._request(
                srv, "GET", "/api/reservations", headers={"Authorization": f"Bearer {self.valid_secret}"}
            )
            self.assertEqual(st2, 200)
            self.assertIn("reservations", body2)
            self.assertEqual(len(body2["reservations"]), 1)

            # 3. 소문자 bearer 및 앞뒤 공백 허용 조회
            st3, body3, _ = self._request(
                srv, "GET", "/api/reservations", headers={"Authorization": f"bearer   {self.valid_secret}  "}
            )
            self.assertEqual(st3, 200)
            self.assertEqual(len(body3["reservations"]), 1)
        finally:
            srv.close()

    def test_health_check_exempt_from_auth(self) -> None:
        """must: 상태 확인 주소를 제외한 모든 요청에 적용한다."""
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.valid_secret))
        try:
            for path in ("/", "/health", "/healthz", "/api/health"):
                # 키 없이도 200 OK
                st, body, _ = self._request(srv, "GET", path)
                self.assertEqual(st, 200, f"Path {path} without key failed")
                self.assertEqual(body.get("status"), "ok")

                # 틀린 키가 있어도 상태 확인은 200 OK
                st_wrong, body_wrong, _ = self._request(
                    srv, "GET", path, headers={"X-Shared-Secret": "wrong-secret"}
                )
                self.assertEqual(st_wrong, 200, f"Path {path} with wrong key failed")
                self.assertEqual(body_wrong.get("status"), "ok")
        finally:
            srv.close()

    def test_all_other_methods_and_endpoints_protected(self) -> None:
        """must: 상태 확인 주소를 제외한 모든 요청(POST, DELETE, 타 API 엔드포인트)에 적용한다."""
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.valid_secret))
        try:
            # POST /api/reservations 보호
            st_post, _, _ = self._request(srv, "POST", "/api/reservations", {"id": "res-unauth"})
            self.assertEqual(st_post, 401)

            # DELETE /api/reservations/123 보호
            st_del, _, _ = self._request(srv, "DELETE", "/api/reservations/res-unauth")
            self.assertEqual(st_del, 401)

            # GET /api/off-periods 보호
            st_op, _, _ = self._request(srv, "GET", "/api/off-periods")
            self.assertEqual(st_op, 401)

            # GET /api/water-tracker 보호
            st_wt, _, _ = self._request(srv, "GET", "/api/water-tracker")
            self.assertEqual(st_wt, 401)

            # GET /api/notification-targets 보호
            st_nt, _, _ = self._request(srv, "GET", "/api/notification-targets")
            self.assertEqual(st_nt, 401)
        finally:
            srv.close()

    def test_unconfigured_secret_rejects_protected_endpoints(self) -> None:
        """서버에 비밀키가 설정되지 않은 경우에도 보호된 엔드포인트는 차단되어야 한다."""
        srv = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=""))
        try:
            # 빈 키로 서버가 실행되었을 때 어떤 키를 넣든 거부
            st1, _, _ = self._request(srv, "GET", "/api/reservations")
            self.assertEqual(st1, 401)
            st2, _, _ = self._request(srv, "GET", "/api/reservations", headers={"X-Shared-Secret": "any-key"})
            self.assertEqual(st2, 401)

            # 상태 확인 주소는 여전히 접근 가능
            st_health, body_h, _ = self._request(srv, "GET", "/health")
            self.assertEqual(st_health, 200)
            self.assertEqual(body_h.get("status"), "ok")
        finally:
            srv.close()

    def test_auth_unit_helpers(self) -> None:
        """server.auth 모듈 함수들의 단위 테스트."""
        # 1. verify_shared_secret
        self.assertTrue(verify_shared_secret("secret123", "secret123"))
        self.assertFalse(verify_shared_secret("secret123", "wrong"))
        self.assertFalse(verify_shared_secret(None, "secret123"))
        self.assertFalse(verify_shared_secret("secret123", None))
        self.assertFalse(verify_shared_secret("", "secret123"))
        self.assertFalse(verify_shared_secret("   ", "secret123"))
        self.assertFalse(verify_shared_secret("secret123", ""))

        # 2. extract_secret_from_headers
        self.assertIsNone(extract_secret_from_headers(None))
        self.assertIsNone(extract_secret_from_headers({}))
        self.assertEqual(extract_secret_from_headers({"X-Shared-Secret": "my-secret"}), "my-secret")
        self.assertEqual(extract_secret_from_headers({"x-shared-secret": " my-secret "}), "my-secret")
        self.assertEqual(extract_secret_from_headers({"Authorization": "Bearer tok123"}), "tok123")
        self.assertEqual(extract_secret_from_headers({"authorization": "bearer tok456"}), "tok456")
        self.assertIsNone(extract_secret_from_headers({"Authorization": "Basic whatever"}))
        self.assertIsNone(extract_secret_from_headers({"Authorization": "Bearer"}))

        # 3. is_health_check_path
        self.assertTrue(is_health_check_path("/"))
        self.assertTrue(is_health_check_path("/health"))
        self.assertTrue(is_health_check_path("/health/"))
        self.assertTrue(is_health_check_path("/healthz"))
        self.assertTrue(is_health_check_path("/api/health"))
        self.assertFalse(is_health_check_path("/api/reservations"))
        self.assertFalse(is_health_check_path("/nonexistent"))


if __name__ == "__main__":
    unittest.main()
