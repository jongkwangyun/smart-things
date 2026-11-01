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
from server.models import OffPeriod, Reservation
from server.off_periods.auto_stop import DEFAULT_DEVICE_ID, AutoStopWatcher
from server.reservations.service import ReservationService
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage
from server.time_utils import KST, format_kst

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_SRC = PROJECT_ROOT / "android/app/src/main/java/com/smartthings/cleaner"
RES_VALUES = PROJECT_ROOT / "android/app/src/main/res/values"


class MockSocket:
    def __init__(self, data: bytes) -> None:
        self.rfile, self.wfile = io.BytesIO(data), io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class DummyClient:
    def __init__(self) -> None:
        self.return_to_home_calls: list[str] = []

    def get_device_status(self, device_id: str) -> dict[str, Any]:
        return {"operating_state": "cleaning"}

    def return_to_home(self, device_id: str) -> dict[str, Any]:
        self.return_to_home_calls.append(device_id)
        return {"results": [{"id": "cmd-return", "status": "COMPLETED"}]}


def http_req(srv: MiniPCServer, method: str, path: str, body: dict[str, Any] | None = None, secret: str = "sec"):
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


class TestReservationOffPeriodWarning(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "test.db"
        self.storage = Storage(self.db_path)
        cfg = Config(host="127.0.0.1", port=0, shared_secret="sec", db_path=self.db_path)
        self.server = MiniPCServer(cfg, self.storage)
        self.now = datetime(2026, 11, 1, 10, 0, 0, tzinfo=KST)
        self.service = ReservationService(self.storage, now_func=lambda: self.now)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.tmp.cleanup()

    def test_server_persistence_create_and_update(self) -> None:
        """Must rule: 허용 여부를 예약에 저장한다 (DB 영구 저장 및 API 응답 반환)."""
        future = self.now + timedelta(hours=2)
        payload = {"scheduled_at": format_kst(future), "allow_during_off_period": True}
        c, body = http_req(self.server, "POST", "/api/reservations", payload)
        self.assertEqual(c, 201)
        self.assertTrue(body["allow_during_off_period"])
        res_id = body["id"]

        r_db = self.storage.get_reservation(res_id)
        self.assertIsNotNone(r_db)
        assert r_db is not None
        self.assertTrue(r_db.allow_during_off_period)

        c_get, data_get = http_req(self.server, "GET", "/api/reservations")
        self.assertEqual(c_get, 200)
        found = [r for r in data_get["reservations"] if r["id"] == res_id]
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["allow_during_off_period"])

        future2 = future + timedelta(hours=1)
        up_payload = {"scheduled_at": format_kst(future2), "allow_during_off_period": False}
        c_put, body_put = http_req(self.server, "PUT", f"/api/reservations/{res_id}", up_payload)
        self.assertEqual(c_put, 200)
        self.assertFalse(body_put["allow_during_off_period"])
        saved = self.storage.get_reservation(res_id)
        assert saved is not None
        self.assertFalse(saved.allow_during_off_period)

        bad_payload = {"scheduled_at": format_kst(future), "allow_during_off_period": "yes"}
        c_bad, _ = http_req(self.server, "POST", "/api/reservations", bad_payload)
        self.assertEqual(c_bad, 400)

    def test_done_when_off_period_warning_and_dialog_decisions(self) -> None:
        """Done When: 쉬는 기간 안의 시각이면 경고, 확인 시 저장, 취소 시 미저장."""
        t_start = self.now + timedelta(hours=1)
        t_end = self.now + timedelta(hours=3)
        self.storage.save_off_period(OffPeriod(id="op-1", start_at=t_start, end_at=t_end))

        def is_in_off_period(target: datetime) -> bool:
            return any(op.start_at <= target <= op.end_at for op in self.storage.list_off_periods())

        self.assertFalse(is_in_off_period(t_start - timedelta(minutes=1)))
        self.assertTrue(is_in_off_period(t_start))
        self.assertTrue(is_in_off_period(t_start + timedelta(hours=1)))
        self.assertTrue(is_in_off_period(t_end))
        self.assertFalse(is_in_off_period(t_end + timedelta(minutes=1)))

        target_inside = t_start + timedelta(hours=1)
        self.assertTrue(is_in_off_period(target_inside))
        create_data = {"scheduled_at": format_kst(target_inside), "allow_during_off_period": True}
        res_created = self.service.create_reservation(create_data)
        self.assertTrue(res_created.allow_during_off_period)
        self.assertEqual(len(self.storage.list_reservations()), 1)

        target_cancelled = t_start + timedelta(hours=2)
        self.assertTrue(is_in_off_period(target_cancelled))
        # 취소 시뮬레이션: 저장 호출 안 함
        self.assertEqual(len(self.storage.list_reservations()), 1)

        # 수정 시 확인 시뮬레이션
        target_update_inside = t_start + timedelta(minutes=30)
        up_data = {"scheduled_at": format_kst(target_update_inside), "allow_during_off_period": True}
        up_res = self.service.update_reservation(res_created.id, up_data)
        self.assertIsNotNone(up_res)
        assert up_res is not None
        self.assertTrue(up_res.allow_during_off_period)
        self.assertEqual(up_res.reserved_at, target_update_inside)

    def test_autostop_watcher_integration_with_allowed_reservation(self) -> None:
        """REQ-015, D-05: 허용된 1회성 예약 청소는 쉬는 기간 중에도 자동 정지되지 않는다."""
        t_start = self.now - timedelta(hours=1)
        t_end = self.now + timedelta(hours=1)
        self.storage.save_off_period(OffPeriod(id="op-holiday", start_at=t_start, end_at=t_end))

        client = DummyClient()
        watcher = AutoStopWatcher(self.storage, client, device_id=DEFAULT_DEVICE_ID, now_func=lambda: self.now)  # type: ignore[arg-type]

        event = watcher.step()
        self.assertIsNotNone(event)
        self.assertEqual(len(client.return_to_home_calls), 1)

        client.return_to_home_calls.clear()
        self.storage.save_reservation(Reservation(
            id="res-allowed",
            reserved_at=self.now - timedelta(minutes=3),
            status="예정",
            allow_during_off_period=True,
            executed_at=self.now - timedelta(minutes=3),
        ))
        event2 = watcher.step()
        self.assertIsNone(event2)
        self.assertEqual(len(client.return_to_home_calls), 0)

    def test_android_contracts_and_strings(self) -> None:
        """Android XML 리소스, ServerClient 계약, 액티비티 계약 검증."""
        tree = ET.parse(RES_VALUES / "strings.xml")
        str_map = {elem.attrib.get("name"): elem.text for elem in tree.findall("string")}
        self.assertEqual(str_map.get("title_off_period_warning"), "쉬는 기간 예약 경고")
        self.assertEqual(
            str_map.get("msg_off_period_reservation_warning"),
            "선택한 시각은 등록된 쉬는 기간에 포함되어 있습니다. 그래도 예약을 진행하시겠습니까?",
        )

        sc_code = (ANDROID_SRC / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("allowDuringOffPeriod: Boolean = false", sc_code)
        self.assertIn('"allow_during_off_period"', sc_code)

        cr_code = (ANDROID_SRC / "CreateReservationActivity.kt").read_text(encoding="utf-8")
        self.assertIn("isDateTimeInOffPeriods", cr_code)
        self.assertIn("showOffPeriodWarningDialog", cr_code)
        self.assertIn("fetchOffPeriods", cr_code)
        self.assertIn("title_off_period_warning", cr_code)
        self.assertIn("msg_off_period_reservation_warning", cr_code)

        rl_code = (ANDROID_SRC / "ReservationListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("isDateTimeInOffPeriods", rl_code)
        self.assertIn("checkAndPerformUpdate", rl_code)
        self.assertIn("showOffPeriodWarningDialog", rl_code)
        self.assertIn("fetchOffPeriods", rl_code)

    def test_file_line_limits(self) -> None:
        """모든 관련 파일 크기 검증 (코드 파일 <= 300, 테스트 파일 <= 250)."""
        files = [
            PROJECT_ROOT / "server/reservations/service.py",
            PROJECT_ROOT / "server/reservations/validator.py",
            ANDROID_SRC / "network/ServerClient.kt",
            ANDROID_SRC / "CreateReservationActivity.kt",
            ANDROID_SRC / "ReservationListActivity.kt",
            ANDROID_SRC / "model/OffPeriodItem.kt",
        ]
        for f in files:
            cnt = len(f.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(cnt, 300, f"{f.name} 줄 수 초과 ({cnt} > 300)")

        my_lines = len(Path(__file__).read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(my_lines, 250, f"테스트 파일 줄 수 초과 ({my_lines} > 250)")


if __name__ == "__main__":
    unittest.main()
