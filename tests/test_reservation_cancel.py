from __future__ import annotations

import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import STATUS_CANCELLED, STATUS_SCHEDULED, STATUS_SUCCESS, Reservation
from server.reservations import ReservationService, ReservationValidationError
from server.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage
from server.time_utils import KST, format_kst, now_kst

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_SRC = PROJECT_ROOT / "android/app/src/main/java/com/smartthings/cleaner"
RES_LAYOUT = PROJECT_ROOT / "android/app/src/main/res/layout"


class MockSocket:
    def __init__(self, data: bytes) -> None:
        self.rfile, self.wfile = io.BytesIO(data), io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class DummyClient:
    def __init__(self) -> None:
        self.started_devices: list[str] = []

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        self.started_devices.append(device_id)
        return {"results": [{"id": f"cmd-{device_id}", "status": "COMPLETED"}]}

    def get_device_status(self, device_id: str) -> dict[str, Any]:
        return {"operating_state": "cleaning"}


def http_req(srv: MiniPCServer, method: str, path: str, body: dict[str, Any] | None = None, secret: str | None = "sec"):
    b = json.dumps(body).encode("utf-8") if body is not None else b""
    h = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"]
    if body is not None:
        h += ["Content-Type: application/json", f"Content-Length: {len(b)}"]
    if secret:
        h.append(f"X-Shared-Secret: {secret}")
    sock = MockSocket("\r\n".join(h).encode("utf-8") + b"\r\n\r\n" + b)
    MiniPCHandler(sock, ("127.0.0.1", 1234), srv)  # type: ignore[arg-type]
    resp = sock.wfile.getvalue().decode("utf-8")
    status = int(resp.split("\r\n")[0].split(" ")[1])
    parts = resp.split("\r\n\r\n", 1)
    data = json.loads(parts[1]) if len(parts) > 1 and parts[1] else {}
    return status, data


class TestReservationCancel(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "test.db"
        self.storage = Storage(self.db)
        self.fixed_now = datetime(2026, 11, 1, 10, 0, 0, tzinfo=KST)
        self.service = ReservationService(self.storage, now_func=lambda: self.fixed_now)
        self.server = MiniPCServer(Config(host="127.0.0.1", port=0, shared_secret="sec", db_path=self.db), self.storage)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.tmp.cleanup()

    def test_service_cancel_success_and_rules(self) -> None:
        """단위 테스트: 예정 예약 취소 성공 및 이미 실행/취소/완료된 예약 취소 불가 (Must rule)."""
        t1 = self.fixed_now + timedelta(hours=1)
        res = self.service.create_reservation({"scheduled_at": format_kst(t1)})

        # 1. 예정 예약 취소 성공
        cancelled = self.service.cancel_reservation(res.id)
        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled.status, STATUS_CANCELLED)
        self.assertEqual(cancelled.updated_at, self.fixed_now)
        stored = self.storage.get_reservation(res.id)
        assert stored is not None
        self.assertEqual(stored.status, STATUS_CANCELLED)

        # 2. Must: 이미 취소된 예약은 다시 취소 불가
        with self.assertRaises(ReservationValidationError) as ctx:
            self.service.cancel_reservation(res.id)
        self.assertIn("이미 실행", str(ctx.exception))

        # 3. Must: 이미 실행된 예약(executed_at is not None)은 취소 불가
        res2 = self.service.create_reservation({"scheduled_at": format_kst(t1)})
        res2.executed_at = self.fixed_now
        self.storage.save_reservation(res2)
        with self.assertRaises(ReservationValidationError) as ctx:
            self.service.cancel_reservation(res2.id)
        self.assertIn("이미 실행", str(ctx.exception))

        # 4. Must: 상태가 성공/완료인 예약은 취소 불가
        res3 = self.service.create_reservation({"scheduled_at": format_kst(t1)})
        res3.status = STATUS_SUCCESS
        self.storage.save_reservation(res3)
        with self.assertRaises(ReservationValidationError):
            self.service.cancel_reservation(res3.id)

        # 5. 존재하지 않는 ID 취소 시 None 반환
        self.assertIsNone(self.service.cancel_reservation("non-existent-id"))

    def test_http_api_cancel_endpoint(self) -> None:
        """HTTP POST /api/reservations/{id}/cancel 검증: 200, 401, 404, 400."""
        t1 = now_kst().replace(second=0, microsecond=0) + timedelta(hours=2)
        res = self.storage.save_reservation(Reservation(id="res-cancel-1", reserved_at=t1, status=STATUS_SCHEDULED))

        # 200 성공
        code, body = http_req(self.server, "POST", f"/api/reservations/{res.id}/cancel")
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], STATUS_CANCELLED)

        # 401 미인증 (REQ-024)
        res_sec = self.storage.save_reservation(
            Reservation(id="res-cancel-sec", reserved_at=t1, status=STATUS_SCHEDULED)
        )
        code, _ = http_req(self.server, "POST", f"/api/reservations/{res_sec.id}/cancel", secret="wrong")
        self.assertEqual(code, 401)

        # 404 미존재
        code, _ = http_req(self.server, "POST", "/api/reservations/nonexistent/cancel")
        self.assertEqual(code, 404)

        # 400 이미 취소된 예약 (Must rule)
        code, body = http_req(self.server, "POST", f"/api/reservations/{res.id}/cancel")
        self.assertEqual(code, 400)
        self.assertIn("이미 실행", body.get("error", ""))

        # 400 이미 실행된 예약 (Must rule)
        res_exec = self.storage.save_reservation(
            Reservation(id="res-cancel-exec", reserved_at=t1, status=STATUS_SCHEDULED, executed_at=now_kst())
        )
        code, body = http_req(self.server, "POST", f"/api/reservations/{res_exec.id}/cancel")
        self.assertEqual(code, 400)

    def test_scheduler_done_when_cancelled_reservation_never_cleans(self) -> None:
        """스케줄러 연동 테스트 (Done When): 미래 예약 취소 시, 그 시각이 지나도 청소기가 출발하지 않음."""
        client = DummyClient()
        current_time = self.fixed_now
        sched_log = Path(self.tmp.name) / "scheduler.log"
        sched = ReservationScheduler(
            storage=self.storage,
            client=client,  # type: ignore[arg-type]
            device_id="dev-vr7m",
            now_func=lambda: current_time,
            log_file=sched_log,
        )

        t_future = self.fixed_now + timedelta(minutes=10)
        res = self.service.create_reservation({"scheduled_at": format_kst(t_future)})

        # 앱에서 예약 취소
        self.service.cancel_reservation(res.id)
        saved = self.storage.get_reservation(res.id)
        assert saved is not None
        self.assertEqual(saved.status, STATUS_CANCELLED)

        # 시각이 예약 시각을 지남 (Done When: 그 시각이 지나도 청소기가 출발하지 않는다)
        current_time = t_future + timedelta(minutes=1)
        executed = sched.step()

        self.assertEqual(len(executed), 0, "취소된 예약은 스케줄러 step에서 반환되지 않아야 함")
        self.assertEqual(len(client.started_devices), 0, "취소된 예약 시각이 지나도 청소기가 출발하지 않아야 함")
        if sched_log.exists():
            log_content = sched_log.read_text(encoding="utf-8")
            self.assertNotIn("cmd=start", log_content, "청소 시작 명령이 로그에 없어야 함")

    def test_android_layout_and_contracts(self) -> None:
        """안드로이드 계약 테스트: 버튼 노출, 모델, 클라이언트 계약."""
        tree = ET.parse(RES_LAYOUT / "item_reservation.xml")
        btn = tree.find(".//com.google.android.material.button.MaterialButton[@{http://schemas.android.com/apk/res/android}id='@+id/btnCancelReservation']")
        self.assertIsNotNone(btn, "item_reservation.xml에 btnCancelReservation 누락")
        assert btn is not None
        self.assertEqual(btn.attrib.get("{http://schemas.android.com/apk/res/android}text"), "취소")
        self.assertEqual(btn.attrib.get("{http://schemas.android.com/apk/res/android}layout_marginStart"), "8dp")

        act_code = (ANDROID_SRC / "ReservationListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("btnCancelReservation", act_code)
        self.assertIn("showCancelConfirmDialog", act_code)
        self.assertIn("performCancelReservation", act_code)
        self.assertIn("serverClient.cancelReservation", act_code)

        client_code = (ANDROID_SRC / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("fun cancelReservation(", client_code)
        self.assertIn("/cancel", client_code)

        model_code = (ANDROID_SRC / "model/CancelReservationResult.kt").read_text(encoding="utf-8")
        self.assertIn("sealed class CancelReservationResult", model_code)
        self.assertIn("data class Success", model_code)
        self.assertIn("data class Failure", model_code)

    def test_file_line_limits(self) -> None:
        """모든 파일 크기 검증 (각 파일 <= 300줄, CancelReservationResult <= 30줄, 본 테스트 <= 250줄)."""
        files_to_check = [
            PROJECT_ROOT / "server/reservations/service.py",
            PROJECT_ROOT / "server/server.py",
            ANDROID_SRC / "network/ServerClient.kt",
            ANDROID_SRC / "ReservationListActivity.kt",
            RES_LAYOUT / "item_reservation.xml",
        ]
        for f in files_to_check:
            cnt = len(f.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(cnt, 300, f"{f.name} 파일 줄 수 초과 ({cnt} > 300)")

        cancel_model = ANDROID_SRC / "model/CancelReservationResult.kt"
        cnt_model = len(cancel_model.read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(cnt_model, 30, f"CancelReservationResult.kt 줄 수 초과 ({cnt_model} > 30)")

        test_cnt = len(Path(__file__).read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(test_cnt, 250, f"테스트 파일 줄 수 초과 ({test_cnt} > 250)")


if __name__ == "__main__":
    unittest.main()
