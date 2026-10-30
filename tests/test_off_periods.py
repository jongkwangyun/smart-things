from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.off_periods import (
    OffPeriodConflictError,
    OffPeriodService,
    OffPeriodValidationError,
    validate_off_period_payload,
)
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage
from server.time_utils import format_kst, now_kst


class MockSocket:
    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class TestOffPeriodValidator(unittest.TestCase):
    """쉬는 기간 등록 검증 로직 단위 테스트 (REQ-011, LB-020)."""

    def setUp(self) -> None:
        self.base_time = now_kst().replace(second=0, microsecond=0)

    def test_non_dict_payload_rejected(self) -> None:
        for invalid in ["not a dict", [1, 2, 3], None, 123]:
            with self.assertRaises(OffPeriodValidationError) as cm:
                validate_off_period_payload(invalid)
            self.assertIn("JSON 객체", str(cm.exception))

    def test_missing_fields_rejected(self) -> None:
        with self.assertRaises(OffPeriodValidationError) as cm:
            validate_off_period_payload({"start_at": format_kst(self.base_time)})
        self.assertIn("'start_at'과 'end_at' 필드가 필요합니다.", str(cm.exception))

        with self.assertRaises(OffPeriodValidationError) as cm:
            validate_off_period_payload({"end_at": format_kst(self.base_time + timedelta(hours=2))})
        self.assertIn("'start_at'과 'end_at' 필드가 필요합니다.", str(cm.exception))

    def test_invalid_iso_datetime_rejected(self) -> None:
        with self.assertRaises(OffPeriodValidationError) as cm:
            validate_off_period_payload({"start_at": "invalid-time", "end_at": format_kst(self.base_time)})
        self.assertIn("유효한 ISO 8601", str(cm.exception))

    def test_non_minute_precision_rejected(self) -> None:
        sec_time = self.base_time.replace(second=15)
        with self.assertRaises(OffPeriodValidationError) as cm:
            validate_off_period_payload({
                "start_at": format_kst(sec_time),
                "end_at": format_kst(self.base_time + timedelta(hours=2)),
            })
        self.assertIn("분 단위여야 합니다", str(cm.exception))

        with self.assertRaises(OffPeriodValidationError) as cm:
            validate_off_period_payload({
                "start_at": format_kst(self.base_time),
                "end_at": format_kst(sec_time + timedelta(hours=2)),
            })
        self.assertIn("분 단위여야 합니다", str(cm.exception))

    def test_end_before_or_equal_to_start_rejected(self) -> None:
        """done_when: 종료가 시작보다 앞서면 저장되지 않고 안내가 보인다 (REQ-011)."""
        # 1. 종료와 시작이 같은 시각인 경우
        with self.assertRaises(OffPeriodValidationError) as cm_equal:
            validate_off_period_payload({
                "start_at": format_kst(self.base_time),
                "end_at": format_kst(self.base_time),
            })
        self.assertEqual(str(cm_equal.exception), "종료 일시는 시작 일시보다 뒤여야 합니다.")

        # 2. 종료가 시작보다 앞선 시각인 경우
        with self.assertRaises(OffPeriodValidationError) as cm_before:
            validate_off_period_payload({
                "start_at": format_kst(self.base_time + timedelta(hours=3)),
                "end_at": format_kst(self.base_time),
            })
        self.assertEqual(str(cm_before.exception), "종료 일시는 시작 일시보다 뒤여야 합니다.")

    def test_valid_payload_accepted(self) -> None:
        start_dt = self.base_time
        end_dt = self.base_time + timedelta(days=3)
        res = validate_off_period_payload({
            "start_at": format_kst(start_dt),
            "end_at": format_kst(end_dt),
            "id": "op-custom-1",
        })
        self.assertEqual(res["id"], "op-custom-1")
        self.assertEqual(res["start_at"], start_dt)
        self.assertEqual(res["end_at"], end_dt)


class TestOffPeriodService(unittest.TestCase):
    """쉬는 기간 서비스 단위 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_off.db"
        self.storage = Storage(self.db_path)
        self.service = OffPeriodService(self.storage)
        self.base_time = now_kst().replace(second=0, microsecond=0)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_create_off_period_instance_and_class_style(self) -> None:
        payload1 = {
            "start_at": format_kst(self.base_time),
            "end_at": format_kst(self.base_time + timedelta(days=1)),
        }
        op1 = self.service.create_off_period(payload1)
        self.assertTrue(op1.id.startswith("off-"))
        self.assertEqual(self.storage.get_off_period(op1.id), op1)

        payload2 = {
            "start_at": format_kst(self.base_time + timedelta(days=2)),
            "end_at": format_kst(self.base_time + timedelta(days=3)),
        }
        op2 = OffPeriodService.create_off_period(self.storage, payload2)
        self.assertTrue(op2.id.startswith("off-"))
        self.assertEqual(self.storage.get_off_period(op2.id), op2)

    def test_duplicate_id_raises_conflict(self) -> None:
        payload = {
            "id": "op-dup-test",
            "start_at": format_kst(self.base_time),
            "end_at": format_kst(self.base_time + timedelta(days=1)),
        }
        self.service.create_off_period(payload)
        with self.assertRaises(OffPeriodConflictError):
            self.service.create_off_period(payload)


class TestOffPeriodServerApi(unittest.TestCase):
    """쉬는 기간 HTTP API 테스트 (REQ-011, REQ-024)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_api_off.db"
        self.secret = "off-secret-1234"
        self.storage = Storage(self.db_path)
        self.config = Config(host="127.0.0.1", port=0, shared_secret=self.secret, db_path=self.db_path)
        self.server = MiniPCServer(self.config, self.storage)
        self.base_time = now_kst().replace(second=0, microsecond=0)

    def tearDown(self) -> None:
        self.server.close()
        self.temp_dir.cleanup()

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None, secret: str | None = None
    ) -> tuple[int, dict[str, Any]]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"]
        if payload is not None:
            lines.extend(["Content-Type: application/json; charset=UTF-8", f"Content-Length: {len(body)}"])
        sec = secret if secret is not None else self.secret
        if sec:
            lines.append(f"X-Shared-Secret: {sec}")
        raw_req = "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body
        sock = MockSocket(raw_req)
        MiniPCHandler(sock, ("127.0.0.1", 12345), self.server)  # type: ignore[arg-type]
        raw_resp = sock.wfile.getvalue().decode("utf-8")
        status_line, rest = raw_resp.split("\r\n", 1)
        resp_body = rest.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in rest else ""
        return int(status_line.split(" ")[1]), (json.loads(resp_body) if resp_body else {})

    def test_post_off_period_invalid_range_returns_400(self) -> None:
        """done_when: 종료가 시작보다 앞서면 400 Bad Request와 안내 문구 반환."""
        payload = {
            "start_at": format_kst(self.base_time + timedelta(hours=5)),
            "end_at": format_kst(self.base_time),
        }
        code, body = self._request("POST", "/api/off-periods", payload)
        self.assertEqual(code, 400)
        self.assertEqual(body.get("error"), "종료 일시는 시작 일시보다 뒤여야 합니다.")

    def test_post_off_period_valid_creates_and_returns_201(self) -> None:
        """done_when: 올바른 시작·종료 일시 등록 성공."""
        payload = {
            "start_at": format_kst(self.base_time),
            "end_at": format_kst(self.base_time + timedelta(days=2)),
        }
        code, body = self._request("POST", "/api/off-periods", payload)
        self.assertEqual(code, 201)
        self.assertTrue(body["id"].startswith("off-"))
        self.assertEqual(body["start_at"], format_kst(self.base_time))
        self.assertEqual(body["end_at"], format_kst(self.base_time + timedelta(days=2)))

    def test_unauthorized_post_rejected_with_401(self) -> None:
        payload = {
            "start_at": format_kst(self.base_time),
            "end_at": format_kst(self.base_time + timedelta(days=1)),
        }
        code, _ = self._request("POST", "/api/off-periods", payload, secret="wrong-secret")
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
