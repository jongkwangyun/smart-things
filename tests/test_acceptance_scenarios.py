from __future__ import annotations

import io
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import STATUS_CANCELLED, STATUS_SUCCESS
from server.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer, create_server
from server.smartthings.client import DeviceStatus
from server.storage import Storage
from server.time_utils import format_kst, now_kst

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAYOUT_DIR = PROJECT_ROOT / "android/app/src/main/res/layout"
MANIFEST_PATH = PROJECT_ROOT / "android/app/src/main/AndroidManifest.xml"


class MockClient:
    def __init__(self, operating_state: str = "idle") -> None:
        self.operating_state = operating_state
        self.started_devices: list[str] = []

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        self.started_devices.append(device_id)
        return {"results": [{"id": f"cmd-{device_id}", "status": "COMPLETED"}]}

    def get_device_status(self, device_id: str) -> DeviceStatus:
        return DeviceStatus(
            device_id=device_id,
            operating_state=self.operating_state,
            raw_status={
                "components": {
                    "main": {
                        "robotCleanerOperatingState": {"operatingState": {"value": self.operating_state}},
                    }
                }
            },
        )


class MockSocket:
    def __init__(self, data: bytes) -> None:
        self.rfile, self.wfile = io.BytesIO(data), io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


def _req(
    server: MiniPCServer,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    secret: str = "acceptance-secret",
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    hdr = f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Shared-Secret: {secret}\r\n"
    if body:
        hdr += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
    sock = MockSocket(hdr.encode("utf-8") + b"\r\n" + body)
    MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
    parts = sock.wfile.getvalue().split(b"\r\n\r\n", 1)
    status_code = int(parts[0].split(b"\r\n")[0].split(b" ")[1])
    data = json.loads(parts[1].decode("utf-8")) if len(parts) > 1 and parts[1] else {}
    return status_code, data


class TestAcceptanceScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "acceptance.db"
        self.secret = "acceptance-secret"
        self.t0 = (now_kst() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_ac1_multi_user_external_reservation(self) -> None:
        """AC-1 / LB-025: 모바일 데이터 환경에서 두 사용자가 각자 예약 후 ±5분 내 청소 시작 성공 판정."""
        storage = Storage(self.db_path)
        cfg = Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret)
        server = MiniPCServer(cfg, storage)
        client = MockClient(operating_state="idle")
        cur_time = [self.t0]
        scheduler = ReservationScheduler(
            storage=storage, client=client, device_id="vr7m-test",  # type: ignore[arg-type]
            poll_interval=0.01, now_func=lambda: cur_time[0],
            log_file=Path(self.temp_dir.name) / "sched.log",
        )
        try:
            t1, t2 = self.t0 + timedelta(minutes=10), self.t0 + timedelta(minutes=30)
            s1, b1 = _req(server, "POST", "/api/reservations", {"scheduled_at": format_kst(t1)}, self.secret)
            s2, b2 = _req(server, "POST", "/api/reservations", {"scheduled_at": format_kst(t2)}, self.secret)
            self.assertEqual(s1, 201)
            self.assertEqual(s2, 201)
            self.assertNotEqual(b1["id"], b2["id"])

            cur_time[0] = t1
            client.operating_state = "cleaning"
            scheduler.step()
            r1 = storage.get_reservation(b1["id"])
            self.assertIsNotNone(r1)
            self.assertEqual(r1.status, STATUS_SUCCESS)  # type: ignore[union-attr]

            cur_time[0] = t2
            scheduler.step()
            r2 = storage.get_reservation(b2["id"])
            self.assertIsNotNone(r2)
            self.assertEqual(r2.status, STATUS_SUCCESS)  # type: ignore[union-attr]

            _, list_body = _req(server, "GET", "/api/reservations", secret=self.secret)
            statuses = {item["id"]: item["status"] for item in list_body["reservations"]}
            self.assertEqual(statuses[b1["id"]], "성공")
            self.assertEqual(statuses[b2["id"]], "성공")
        finally:
            server.close()

    def test_lb033_equal_permissions_bidirectional(self) -> None:
        """REQ-010 / LB-033: 두 사용자 동일 권한(수정·취소·삭제) 양방향 대칭 확인 (D-09)."""
        server = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret))
        try:
            # 1. 사용자 A 생성 -> 사용자 B 수정/취소/삭제
            t_r1 = format_kst(self.t0 + timedelta(hours=1))
            t_u1 = format_kst(self.t0 + timedelta(hours=2))
            _, res_a = _req(server, "POST", "/api/reservations", {"scheduled_at": t_r1})
            _, op_a = _req(server, "POST", "/api/off-periods", {
                "start_at": format_kst(self.t0 + timedelta(days=1)), "end_at": format_kst(self.t0 + timedelta(days=2))
            })
            code_u1, _ = _req(server, "PUT", f"/api/reservations/{res_a['id']}", {"scheduled_at": t_u1})
            code_c1, b_c1 = _req(server, "POST", f"/api/reservations/{res_a['id']}/cancel")
            code_d1, _ = _req(server, "DELETE", f"/api/off-periods/{op_a['id']}")
            self.assertEqual((code_u1, code_c1, code_d1), (200, 200, 200))
            self.assertEqual(b_c1["status"], STATUS_CANCELLED)

            # 2. 반대 방향: 사용자 B 생성 -> 사용자 A 수정/취소/삭제
            t_r2 = format_kst(self.t0 + timedelta(hours=3))
            t_u2 = format_kst(self.t0 + timedelta(hours=4))
            _, res_b = _req(server, "POST", "/api/reservations", {"scheduled_at": t_r2})
            _, op_b = _req(server, "POST", "/api/off-periods", {
                "start_at": format_kst(self.t0 + timedelta(days=3)), "end_at": format_kst(self.t0 + timedelta(days=4))
            })
            code_u2, _ = _req(server, "PUT", f"/api/reservations/{res_b['id']}", {"scheduled_at": t_u2})
            code_c2, b_c2 = _req(server, "POST", f"/api/reservations/{res_b['id']}/cancel")
            code_d2, _ = _req(server, "DELETE", f"/api/off-periods/{op_b['id']}")
            self.assertEqual((code_u2, code_c2, code_d2), (200, 200, 200))
            self.assertEqual(b_c2["status"], STATUS_CANCELLED)
        finally:
            server.close()

    def test_ac6_lb034_reboot_persistence_and_execution(self) -> None:
        """AC-6 / LB-034: 재부팅 후 SQLite 유지 및 예정 시각 정상 청소 실행 (중복 방지)."""
        target_t = self.t0 + timedelta(minutes=15)
        srv1 = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret))
        _, res_info = _req(srv1, "POST", "/api/reservations", {"scheduled_at": format_kst(target_t)})
        _req(srv1, "POST", "/api/off-periods", {
            "start_at": format_kst(self.t0 + timedelta(days=1)), "end_at": format_kst(self.t0 + timedelta(days=2))
        })
        srv1.close()

        srv2 = create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret))
        client = MockClient(operating_state="cleaning")
        cur_time = [self.t0]
        sched2 = ReservationScheduler(
            storage=srv2.storage, client=client, device_id="vr7m-reboot",  # type: ignore[arg-type]
            poll_interval=0.01, now_func=lambda: cur_time[0],
            log_file=Path(self.temp_dir.name) / "sched2.log",
        )
        try:
            _, list_res = _req(srv2, "GET", "/api/reservations")
            _, list_ops = _req(srv2, "GET", "/api/off-periods")
            self.assertEqual(len(list_res["reservations"]), 1)
            self.assertEqual(len(list_ops["off_periods"]), 1)

            cur_time[0] = target_t
            sched2.step()
            self.assertEqual(len(client.started_devices), 1)
            item = srv2.storage.get_reservation(res_info["id"])
            self.assertIsNotNone(item)
            self.assertEqual(item.status, STATUS_SUCCESS)  # type: ignore[union-attr]

            cur_time[0] = target_t + timedelta(minutes=1)
            sched2.step()
            self.assertEqual(len(client.started_devices), 1)
        finally:
            srv2.close()

    def test_s21_and_fold8_layout_compatibility(self) -> None:
        """REQ-028, REQ-029: S21+ 및 Fold8(접힘·펼침) 적응형 레이아웃 및 환경 설정 검증."""
        activity_layouts = [
            "activity_main.xml", "activity_create_reservation.xml",
            "activity_reservation_list.xml", "activity_create_off_period.xml",
            "activity_off_period_list.xml", "activity_water_management.xml",
        ]
        ns = {"app": "http://schemas.android.com/apk/res-auto", "android": "http://schemas.android.com/apk/res/android"}
        for xml_name in activity_layouts:
            tree = ET.parse(LAYOUT_DIR / xml_name)
            root = tree.getroot()
            self.assertTrue(root.tag.endswith("ScrollView"), f"{xml_name} 루트가 ScrollView가 아님")
            self.assertEqual(root.attrib.get(f"{{{ns['android']}}}fillViewport"), "true")
            has_w = any(
                elem.attrib.get(f"{{{ns['app']}}}layout_constraintWidth_max") == "640dp" for elem in root.iter()
            )
            self.assertTrue(has_w, f"{xml_name}에 layout_constraintWidth_max=640dp 부재")

        m_root = ET.parse(MANIFEST_PATH).getroot()
        for act in m_root.findall(".//activity"):
            cfg = act.attrib.get(f"{{{ns['android']}}}configChanges", "")
            for key in ("orientation", "screenSize", "smallestScreenSize", "screenLayout"):
                self.assertIn(key, cfg, f"{act.attrib.get(f'{{{ns['android']}}}name')} configChanges 누락: {key}")
            self.assertEqual(act.attrib.get(f"{{{ns['android']}}}windowSoftInputMode"), "adjustResize")
