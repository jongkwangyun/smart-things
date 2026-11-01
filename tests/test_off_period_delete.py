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
from server.off_periods import AutoStopWatcher, OffPeriodService
from server.server import MiniPCHandler, MiniPCServer
from server.smartthings.client import DeviceStatus
from server.storage import Storage
from server.time_utils import KST, format_kst

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


class MockSmartThingsClient:
    def __init__(self, status: DeviceStatus | None = None) -> None:
        self.status = status or DeviceStatus(
            device_id="dev-vr7m", operating_state="cleaning", battery=90, is_online=True
        )
        self.return_to_home_calls: list[str] = []

    def get_device_status(self, device_id: str) -> DeviceStatus:
        return self.status

    def return_to_home(self, device_id: str) -> dict[str, Any]:
        self.return_to_home_calls.append(device_id)
        return {"results": [{"id": f"cmd-stop-{device_id}", "status": "COMPLETED"}]}


def http_req(srv: MiniPCServer, method: str, path: str, secret: str | None = "sec", body: dict[str, Any] | None = None):
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


class TestOffPeriodDelete(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "test_delete.db"
        self.storage = Storage(self.db)
        self.fixed_now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=KST)
        self.service = OffPeriodService(self.storage, now_func=lambda: self.fixed_now)
        self.server = MiniPCServer(Config(host="127.0.0.1", port=0, shared_secret="sec", db_path=self.db), self.storage)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.tmp.cleanup()

    def test_http_delete_endpoint(self) -> None:
        """DELETE /api/off-periods/{op_id} API 검증: 200, 404, 401."""
        op = self.service.create_off_period({
            "start_at": format_kst(self.fixed_now - timedelta(hours=1)),
            "end_at": format_kst(self.fixed_now + timedelta(hours=2)),
        })
        self.assertIsNotNone(self.storage.get_off_period(op.id))

        # 401 미인증 (REQ-024)
        c401, _ = http_req(self.server, "DELETE", f"/api/off-periods/{op.id}", secret="wrong-secret")
        self.assertEqual(c401, 401)
        self.assertIsNotNone(self.storage.get_off_period(op.id))

        # 200 성공
        c200, b200 = http_req(self.server, "DELETE", f"/api/off-periods/{op.id}")
        self.assertEqual(c200, 200)
        self.assertTrue(b200.get("deleted"))
        self.assertEqual(b200.get("id"), op.id)
        self.assertIsNone(self.storage.get_off_period(op.id))

        # 404 이미 삭제되었거나 존재하지 않는 ID
        c404, _ = http_req(self.server, "DELETE", f"/api/off-periods/{op.id}")
        self.assertEqual(c404, 404)
        c404_2, _ = http_req(self.server, "DELETE", "/api/off-periods/nonexistent-id")
        self.assertEqual(c404_2, 404)

    def test_done_when_and_must_immediate_restriction_lifted(self) -> None:
        """Done When & Must: 진행 중 쉬는 기간 삭제 시 목록에서 사라지고 직후 청소 시작되어도 정지 안 됨."""
        client = MockSmartThingsClient()
        watcher = AutoStopWatcher(
            storage=self.storage,
            client=client,  # type: ignore[arg-type]
            device_id="dev-vr7m",
            now_func=lambda: self.fixed_now,
        )

        # 1. 현재 시각을 포함하는 진행 중인 쉬는 기간 등록
        op = self.service.create_off_period({
            "start_at": format_kst(self.fixed_now - timedelta(hours=2)),
            "end_at": format_kst(self.fixed_now + timedelta(hours=3)),
        })
        self.assertEqual(watcher.get_active_off_period(self.fixed_now), op)

        # 2. 삭제 전: 청소기 시작 감지 시 자동 정지 실행 (return_to_home 호출, AutoStopEvent 생성)
        client.status.operating_state = "cleaning"
        event = watcher.step(self.fixed_now)
        self.assertIsNotNone(event)
        self.assertEqual(len(client.return_to_home_calls), 1)
        self.assertEqual(len(self.storage.list_auto_stop_events()), 1)

        # 3. HTTP DELETE /api/off-periods/{id}로 쉬는 기간 삭제
        del_code, del_body = http_req(self.server, "DELETE", f"/api/off-periods/{op.id}")
        self.assertEqual(del_code, 200)
        self.assertTrue(del_body.get("deleted"))

        # 4. Done When: 목록에서 사라짐 확인 (GET /api/off-periods 결과 빈 목록)
        get_code, get_body = http_req(self.server, "GET", "/api/off-periods")
        self.assertEqual(get_code, 200)
        self.assertEqual(len(get_body.get("off_periods", [])), 0)
        self.assertIsNone(self.storage.get_off_period(op.id))

        # 5. Must: 삭제 즉시 제한 해제 (get_active_off_period()가 즉시 None 반환)
        self.assertIsNone(watcher.get_active_off_period(self.fixed_now))

        # 6. 직후 반복 예약 청소가 시작되어도 정지되지 않는다 (Done When)
        client.return_to_home_calls.clear()
        client.status.operating_state = "cleaning"
        step_result = watcher.step(self.fixed_now + timedelta(seconds=10))

        self.assertIsNone(step_result, "쉬는 기간 삭제 직후 청소 시작 시 watcher.step은 None 반환해야 함")
        self.assertEqual(len(client.return_to_home_calls), 0, "삭제 직후 청소기 return_to_home 호출되지 않아야 함")
        self.assertEqual(len(self.storage.list_auto_stop_events()), 1, "새 AutoStopEvent가 생성되지 않아야 함")

    def test_android_layout_and_contracts(self) -> None:
        """안드로이드 계약 테스트: 삭제 버튼, 다이얼로그 바인딩, 네트워크 모델 계약."""
        tree = ET.parse(RES_LAYOUT / "item_off_period.xml")
        btn = tree.find(".//com.google.android.material.button.MaterialButton[@{http://schemas.android.com/apk/res/android}id='@+id/btnDeleteOffPeriod']")
        self.assertIsNotNone(btn, "item_off_period.xml에 btnDeleteOffPeriod 누락")
        assert btn is not None
        self.assertEqual(btn.attrib.get("{http://schemas.android.com/apk/res/android}text"), "삭제")

        act_code = (ANDROID_SRC / "OffPeriodListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("btnDeleteOffPeriod", act_code)
        self.assertIn("showDeleteConfirmDialog", act_code)
        self.assertIn("performDeleteOffPeriod", act_code)
        self.assertIn("serverClient.deleteOffPeriod", act_code)
        self.assertIn("이 쉬는 기간을 삭제하시겠습니까?", act_code)

        client_code = (ANDROID_SRC / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("fun deleteOffPeriod(", client_code)
        self.assertIn("/api/off-periods/", client_code)

        model_code = (ANDROID_SRC / "model/DeleteOffPeriodResult.kt").read_text(encoding="utf-8")
        self.assertIn("sealed class DeleteOffPeriodResult", model_code)
        self.assertIn("data class Success(val id: String)", model_code)
        self.assertIn("data class Failure(val statusCode: Int, val message: String)", model_code)

    def test_file_line_limits(self) -> None:
        """모든 파일 크기 검증 (각 파일 <= 300줄, DeleteOffPeriodResult <= 25줄, 본 테스트 <= 250줄)."""
        files_to_check = [
            PROJECT_ROOT / "server/server.py",
            PROJECT_ROOT / "server/off_periods/service.py",
            PROJECT_ROOT / "server/off_periods/auto_stop.py",
            ANDROID_SRC / "network/ServerClient.kt",
            ANDROID_SRC / "OffPeriodListActivity.kt",
            RES_LAYOUT / "item_off_period.xml",
        ]
        for f in files_to_check:
            cnt = len(f.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(cnt, 300, f"{f.name} 파일 줄 수 초과 ({cnt} > 300)")

        server_client = ANDROID_SRC / "network/ServerClient.kt"
        cnt_sc = len(server_client.read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(cnt_sc, 295, f"ServerClient.kt 줄 수 초과 ({cnt_sc} > 295)")

        del_model = ANDROID_SRC / "model/DeleteOffPeriodResult.kt"
        cnt_model = len(del_model.read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(cnt_model, 25, f"DeleteOffPeriodResult.kt 줄 수 초과 ({cnt_model} > 25)")

        test_cnt = len(Path(__file__).read_text(encoding="utf-8").splitlines())
        self.assertLessEqual(test_cnt, 250, f"테스트 파일 줄 수 초과 ({test_cnt} > 250)")


if __name__ == "__main__":
    unittest.main()
