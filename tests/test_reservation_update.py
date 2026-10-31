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


class TestReservationUpdate(unittest.TestCase):
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

    def test_service_update_success_and_rules(self) -> None:
        """단위 테스트: 미래 시각 수정 성공, 과거 시각 실패, 실행/완료 예약 수정 불가 (Must rule)."""
        t1 = self.fixed_now + timedelta(hours=1)
        res = self.service.create_reservation({"scheduled_at": format_kst(t1)})

        # 1. 미래 시각 수정 성공
        t2 = self.fixed_now + timedelta(hours=2)
        payload = {"scheduled_at": format_kst(t2), "allow_during_off_period": True}
        updated = self.service.update_reservation(res.id, payload)
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.reserved_at, t2)
        self.assertTrue(updated.allow_during_off_period)
        stored = self.storage.get_reservation(res.id)
        assert stored is not None
        self.assertEqual(stored.reserved_at, t2)

        # 2. 과거 시각 수정 실패 (REQ-001)
        past = self.fixed_now - timedelta(minutes=10)
        with self.assertRaises(ReservationValidationError):
            self.service.update_reservation(res.id, {"scheduled_at": format_kst(past)})

        # 3. Must: 이미 실행된 예약(executed_at is not None)은 수정 불가
        updated.executed_at = self.fixed_now
        self.storage.save_reservation(updated)
        with self.assertRaises(ReservationValidationError) as ctx:
            self.service.update_reservation(res.id, {"scheduled_at": format_kst(self.fixed_now + timedelta(hours=3))})
        self.assertIn("이미 실행", str(ctx.exception))

        # 4. Must: 상태가 '예정'이 아닌 예약(취소, 성공 등)은 수정 불가
        res2 = self.service.create_reservation({"scheduled_at": format_kst(t1)})
        res2.status = STATUS_CANCELLED
        self.storage.save_reservation(res2)
        with self.assertRaises(ReservationValidationError):
            self.service.update_reservation(res2.id, {"scheduled_at": format_kst(t2)})

        # 5. 존재하지 않는 ID 수정 시 None 반환
        self.assertIsNone(self.service.update_reservation("non-existent-id", {"scheduled_at": format_kst(t2)}))

    def test_http_api_put_endpoints(self) -> None:
        """HTTP PUT /api/reservations/{id} 검증: 200, 401, 404, 400."""
        t1 = now_kst().replace(second=0, microsecond=0) + timedelta(hours=2)
        res = self.storage.save_reservation(Reservation(id="res-http-1", reserved_at=t1, status=STATUS_SCHEDULED))

        # 200 성공
        t2 = t1 + timedelta(hours=1)
        code, body = http_req(self.server, "PUT", f"/api/reservations/{res.id}", {"scheduled_at": format_kst(t2)})
        self.assertEqual(code, 200)
        self.assertEqual(body["scheduled_at"], format_kst(t2))

        # 401 미인증 (REQ-024)
        up_payload = {"scheduled_at": format_kst(t2)}
        code, _ = http_req(self.server, "PUT", f"/api/reservations/{res.id}", up_payload, secret="wrong")
        self.assertEqual(code, 401)

        # 404 미존재
        code, _ = http_req(self.server, "PUT", "/api/reservations/nonexistent", {"scheduled_at": format_kst(t2)})
        self.assertEqual(code, 404)

        # 400 과거 시각
        past = now_kst() - timedelta(hours=1)
        code, _ = http_req(self.server, "PUT", f"/api/reservations/{res.id}", {"scheduled_at": format_kst(past)})
        self.assertEqual(code, 400)

        # 400 이미 실행된 예약 (Must rule)
        res.executed_at = now_kst()
        self.storage.save_reservation(res)
        code, _ = http_req(self.server, "PUT", f"/api/reservations/{res.id}", {"scheduled_at": format_kst(t2)})
        self.assertEqual(code, 400)

    def test_scheduler_execution_after_time_update(self) -> None:
        """스케줄러 연동 테스트 (Done When): T1 예약 생성 -> T2로 수정 시, T1에는 미실행, T2에 실제 청소 실행."""
        client = DummyClient()
        current_time = self.fixed_now
        sched = ReservationScheduler(
            storage=self.storage,
            client=client,  # type: ignore[arg-type]
            device_id="dev-vr7m",
            now_func=lambda: current_time,
            log_file=Path(self.tmp.name) / "sched.log",
        )

        t1 = self.fixed_now + timedelta(minutes=10)
        res = self.service.create_reservation({"scheduled_at": format_kst(t1)})

        # T1 -> T2로 수정
        t2 = self.fixed_now + timedelta(minutes=20)
        self.service.update_reservation(res.id, {"scheduled_at": format_kst(t2)})

        # 시각이 T1에 도달했을 때: T1에는 실행되지 않아야 함
        current_time = t1
        sched.step()
        self.assertEqual(len(client.started_devices), 0, "T1 시점에는 예약이 T2로 미뤄졌으므로 실행되면 안 됨")
        saved_t1 = self.storage.get_reservation(res.id)
        assert saved_t1 is not None
        self.assertIsNone(saved_t1.executed_at)

        # 시각이 T2에 도달했을 때: T2에 실행되어야 함
        current_time = t2
        sched.step()
        self.assertEqual(len(client.started_devices), 1, "T2 시점에 청소 실행되어야 함")
        self.assertIn("dev-vr7m", client.started_devices)
        saved = self.storage.get_reservation(res.id)
        assert saved is not None
        self.assertIsNotNone(saved.executed_at)
        self.assertEqual(saved.status, STATUS_SUCCESS)

    def test_android_layout_and_contracts(self) -> None:
        """안드로이드 계약 테스트: 버튼 노출(예정: VISIBLE, 과거: GONE), 모델, 클라이언트 계약."""
        tree = ET.parse(RES_LAYOUT / "item_reservation.xml")
        btn = tree.find(".//com.google.android.material.button.MaterialButton[@{http://schemas.android.com/apk/res/android}id='@+id/btnEditReservation']")
        self.assertIsNotNone(btn, "item_reservation.xml에 btnEditReservation 누락")
        assert btn is not None
        self.assertEqual(btn.attrib.get("{http://schemas.android.com/apk/res/android}text"), "수정")

        act_code = (ANDROID_SRC / "ReservationListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("btnEditReservation", act_code)
        self.assertIn("item.isUpcoming()", act_code)
        self.assertIn("View.VISIBLE", act_code)
        self.assertIn("View.GONE", act_code)
        self.assertIn("showEditPickers", act_code)
        self.assertIn("serverClient.updateReservation", act_code)

        client_code = (ANDROID_SRC / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("fun updateReservation(", client_code)
        self.assertIn('"PUT"', client_code)
        self.assertIn("/api/reservations/", client_code)

        model_code = (ANDROID_SRC / "model/UpdateReservationResult.kt").read_text(encoding="utf-8")
        self.assertIn("sealed class UpdateReservationResult", model_code)
        self.assertIn("data class Success", model_code)
        self.assertIn("data class Failure", model_code)

    def test_file_line_limits(self) -> None:
        """모든 파일 크기 검증 (각 파일 <= 300줄, 본 테스트 <= 250줄)."""
        files_to_check = [
            PROJECT_ROOT / "server/reservations/service.py",
            PROJECT_ROOT / "server/reservations/validator.py",
            PROJECT_ROOT / "server/server.py",
            ANDROID_SRC / "model/UpdateReservationResult.kt",
            ANDROID_SRC / "network/ServerClient.kt",
            ANDROID_SRC / "ReservationListActivity.kt",
            RES_LAYOUT / "item_reservation.xml",
        ]
        for f in files_to_check:
            cnt = len(f.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(cnt, 300, f"{f.name} 파일 줄 수 초과 ({cnt} > 300)")

        test_cnt = len(Path(__file__).read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(test_cnt, 250, f"테스트 파일 줄 수 초과 ({test_cnt} > 250)")


if __name__ == "__main__":
    unittest.main()
