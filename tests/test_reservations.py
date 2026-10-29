from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import STATUS_SCHEDULED
from server.reservations import (
    FORBIDDEN_MODE_ZONE_KEYS,
    ReservationConflictError,
    ReservationService,
    ReservationValidationError,
    validate_reservation_create_payload,
)
from server.server import MiniPCHandler, create_server
from server.storage import Storage
from server.time_utils import KST, format_kst, now_kst


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


class TestReservationValidationAndService(unittest.TestCase):
    def setUp(self) -> None:
        self.fixed_now = datetime(2026, 10, 30, 10, 0, 0, 0, tzinfo=KST)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "res_test.db"
        self.storage = Storage(self.db_path)
        self.service = ReservationService(self.storage, now_func=lambda: self.fixed_now)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_validate_valid_payload_scheduled_at(self) -> None:
        payload = {"scheduled_at": "2026-10-30T10:15:00+09:00"}
        res = validate_reservation_create_payload(payload, now_func=lambda: self.fixed_now)
        expected_dt = datetime(2026, 10, 30, 10, 15, 0, 0, tzinfo=KST)
        self.assertEqual(res["reserved_at"], expected_dt)
        self.assertFalse(res["allow_during_off_period"])
        self.assertIsNone(res["id"])

    def test_validate_valid_payload_reserved_at(self) -> None:
        payload = {"reserved_at": "2026-10-30T11:00:00+09:00", "id": "my-res-1", "allow_during_off_period": True}
        res = validate_reservation_create_payload(payload, now_func=lambda: self.fixed_now)
        self.assertEqual(res["id"], "my-res-1")
        self.assertTrue(res["allow_during_off_period"])

    def test_validate_utc_timezone_converted_to_kst(self) -> None:
        # UTC 01:30 is KST 10:30
        payload = {"scheduled_at": "2026-10-30T01:30:00Z"}
        res = validate_reservation_create_payload(payload, now_func=lambda: self.fixed_now)
        self.assertEqual(res["reserved_at"], datetime(2026, 10, 30, 10, 30, 0, 0, tzinfo=KST))

    def test_validate_non_minute_precision_rejected(self) -> None:
        # must: 분 단위 일시만 받는다
        with self.assertRaises(ReservationValidationError) as ctx:
            validate_reservation_create_payload(
                {"scheduled_at": "2026-10-30T10:15:30+09:00"},
                now_func=lambda: self.fixed_now,
            )
        self.assertIn("분 단위", str(ctx.exception))

        with self.assertRaises(ReservationValidationError):
            validate_reservation_create_payload(
                {"scheduled_at": "2026-10-30T10:15:00.123456+09:00"},
                now_func=lambda: self.fixed_now,
            )

    def test_validate_past_and_equal_datetime_rejected(self) -> None:
        # REQ-001: 현재보다 과거 시각은 예약할 수 없어야 한다
        past_str = "2026-10-30T09:59:00+09:00"
        with self.assertRaises(ReservationValidationError) as ctx:
            validate_reservation_create_payload(
                {"scheduled_at": past_str},
                now_func=lambda: self.fixed_now,
            )
        self.assertIn("미래", str(ctx.exception))

        # 현재 시각과 동일한 경우도 거부
        equal_str = "2026-10-30T10:00:00+09:00"
        with self.assertRaises(ReservationValidationError) as ctx_eq:
            validate_reservation_create_payload(
                {"scheduled_at": equal_str},
                now_func=lambda: self.fixed_now,
            )
        self.assertIn("미래", str(ctx_eq.exception))

    def test_validate_forbidden_mode_and_zone_rejected(self) -> None:
        # D-03: 모드·구역 선택 없음
        for key in FORBIDDEN_MODE_ZONE_KEYS:
            with self.subTest(key=key):
                payload = {"scheduled_at": "2026-10-30T10:15:00+09:00", key: "living_room"}
                with self.assertRaises(ReservationValidationError) as ctx:
                    validate_reservation_create_payload(payload, now_func=lambda: self.fixed_now)
                self.assertIn("청소 모드 및 구역 선택은 지원되지 않습니다", str(ctx.exception))

    def test_validate_missing_or_invalid_fields(self) -> None:
        with self.assertRaises(ReservationValidationError):
            validate_reservation_create_payload("not-a-dict")
        with self.assertRaises(ReservationValidationError):
            validate_reservation_create_payload({})
        with self.assertRaises(ReservationValidationError):
            validate_reservation_create_payload({"scheduled_at": 12345})
        with self.assertRaises(ReservationValidationError):
            validate_reservation_create_payload({"scheduled_at": "not-a-datetime"})
        with self.assertRaises(ReservationValidationError):
            validate_reservation_create_payload(
                {"scheduled_at": "2026-10-30T10:15:00+09:00", "allow_during_off_period": "yes"}
            )
        with self.assertRaises(ReservationValidationError):
            validate_reservation_create_payload(
                {"scheduled_at": "2026-10-30T10:15:00+09:00", "id": "   "}
            )

    def test_service_create_reservation_success_and_defaults(self) -> None:
        payload = {
            "scheduled_at": "2026-10-30T10:15:00+09:00",
            "status": "성공",  # 임의 상태를 넣어도 '예정'으로 강제됨
        }
        res = self.service.create_reservation(payload)
        self.assertTrue(res.id.startswith("res-"))
        self.assertEqual(res.status, STATUS_SCHEDULED)
        self.assertEqual(res.reserved_at, datetime(2026, 10, 30, 10, 15, 0, 0, tzinfo=KST))
        self.assertIsNone(res.failure_reason)
        self.assertIsNone(res.last_device_state)
        self.assertEqual(res.created_at, self.fixed_now)

        # 저장소 확인
        saved = self.storage.get_reservation(res.id)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.status, STATUS_SCHEDULED)

    def test_service_duplicate_id_raises_conflict(self) -> None:
        payload = {"id": "unique-res-id", "scheduled_at": "2026-10-30T10:15:00+09:00"}
        self.service.create_reservation(payload)
        with self.assertRaises(ReservationConflictError):
            self.service.create_reservation(payload)


class TestReservationAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "server_res_test.db"
        self.test_secret = "test-secret-reservations-1234"
        cfg = Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.test_secret)
        self.server = create_server(cfg)

    def tearDown(self) -> None:
        self.server.close()
        self.temp_dir.cleanup()

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        secret: str | None = "test-secret-reservations-1234",
    ) -> tuple[int, dict[str, Any]]:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
        req_headers: dict[str, str] = {}
        if payload is not None:
            req_headers["Content-Type"] = "application/json"
            req_headers["Content-Length"] = str(len(body_bytes))
        if secret is not None:
            req_headers["X-Shared-Secret"] = secret
        for k, v in req_headers.items():
            lines.append(f"{k}: {v}")
        raw_req = "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body_bytes
        sock = MockSocket(raw_req)
        MiniPCHandler(sock, ("127.0.0.1", 12345), self.server)  # type: ignore[arg-type]

        raw_resp = sock.wfile.getvalue().decode("utf-8")
        status_line, rest = raw_resp.split("\r\n", 1)
        status_code = int(status_line.split(" ")[1])
        parts = rest.split("\r\n\r\n", 1)
        body_text = parts[1] if len(parts) > 1 else ""
        data = json.loads(body_text) if body_text else {}
        return status_code, data

    def test_done_when_future_creates_and_appears_in_list_as_scheduled(self) -> None:
        """done_when: 미래 일시로 생성 요청하면 저장되어 예약 목록 응답에 '예정' 상태로 나온다."""
        future_dt = (now_kst() + timedelta(hours=2)).replace(second=0, microsecond=0)
        future_str = format_kst(future_dt)

        # 1. 예약 생성 요청
        create_payload = {"scheduled_at": future_str}
        status, body = self._request("POST", "/api/reservations", create_payload)
        self.assertEqual(status, 201)
        self.assertEqual(body["status"], STATUS_SCHEDULED)
        self.assertEqual(body["scheduled_at"], future_str)
        self.assertEqual(body["reserved_at"], future_str)
        created_id = body["id"]

        # 2. 예약 목록 조회에서 '예정' 상태로 확인
        list_status, list_body = self._request("GET", "/api/reservations")
        self.assertEqual(list_status, 200)
        reservations = list_body.get("reservations", [])
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["id"], created_id)
        self.assertEqual(reservations[0]["status"], "예정")
        self.assertEqual(reservations[0]["reserved_at"], future_str)

        # 3. 단건 조회에서도 '예정' 확인
        get_status, get_body = self._request("GET", f"/api/reservations/{created_id}")
        self.assertEqual(get_status, 200)
        self.assertEqual(get_body["id"], created_id)
        self.assertEqual(get_body["status"], "예정")

    def test_done_when_past_datetime_returns_rejection_error(self) -> None:
        """done_when: 과거 일시 요청은 거부 메시지가 돌아온다."""
        past_dt = (now_kst() - timedelta(minutes=10)).replace(second=0, microsecond=0)
        past_str = format_kst(past_dt)

        status, body = self._request("POST", "/api/reservations", {"scheduled_at": past_str})
        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertIn("미래", body["error"])

        # 저장소에 등록되지 않음을 확인
        list_status, list_body = self._request("GET", "/api/reservations")
        self.assertEqual(list_status, 200)
        self.assertEqual(len(list_body.get("reservations", [])), 0)

    def test_non_minute_precision_returns_400(self) -> None:
        # must: 분 단위 일시만 받는다
        second_dt = (now_kst() + timedelta(hours=1)).replace(second=30, microsecond=0)
        status, body = self._request("POST", "/api/reservations", {"scheduled_at": format_kst(second_dt)})
        self.assertEqual(status, 400)
        self.assertIn("분 단위", body.get("error", ""))

    def test_mode_and_zone_payload_returns_400(self) -> None:
        # must: 모드·구역 선택 없음 (D-03)
        future_dt = (now_kst() + timedelta(hours=1)).replace(second=0, microsecond=0)
        status, body = self._request(
            "POST",
            "/api/reservations",
            {"scheduled_at": format_kst(future_dt), "mode": "mop_only"},
        )
        self.assertEqual(status, 400)
        self.assertIn("청소 모드 및 구역 선택은 지원되지 않습니다", body.get("error", ""))

    def test_unauthorized_request_rejected(self) -> None:
        future_dt = (now_kst() + timedelta(hours=1)).replace(second=0, microsecond=0)
        status, _ = self._request("POST", "/api/reservations", {"scheduled_at": format_kst(future_dt)}, secret=None)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
