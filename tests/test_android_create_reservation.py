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
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage
from server.time_utils import format_kst, now_kst

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android"
RES_LAYOUT = ANDROID_ROOT / "app/src/main/res/layout"
RES_VALUES = ANDROID_ROOT / "app/src/main/res/values"
SRC_JAVA = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"
MANIFEST_PATH = ANDROID_ROOT / "app/src/main/AndroidManifest.xml"


class MockSocket:
    """테스트용 인메모리 HTTP 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class SimulatedCreateReservationUiState:
    """CreateReservationActivity의 UI 상태 전이 시뮬레이터 (LB-013, SPEC F-1)."""

    def __init__(self) -> None:
        self.warning_visible: bool = False
        self.warning_message: str = ""
        self.confirmation_visible: bool = False
        self.confirmation_message: str = ""
        self.confirmation_dialog_shown: bool = False
        self.dialog_message: str = ""
        self.server_request_sent: bool = False

    def on_date_time_changed(self, selected_dt: datetime, now_dt: datetime) -> None:
        if selected_dt <= now_dt:
            self.warning_visible = True
            self.warning_message = "과거 시각은 예약할 수 없습니다."
            self.confirmation_visible = False
        else:
            self.warning_visible = False

    def on_save_clicked(
        self, selected_dt: datetime, now_dt: datetime, server: MiniPCServer | None, server_url: str, secret: str
    ) -> bool:
        if selected_dt <= now_dt:
            self.warning_visible = True
            self.warning_message = "과거 시각은 예약할 수 없습니다."
            self.confirmation_visible = False
            self.server_request_sent = False
            return False

        self.server_request_sent = True
        clean_url = server_url.strip().rstrip("/")
        if not clean_url or not secret.strip() or server is None:
            self.warning_visible = True
            self.warning_message = "서버에 연결할 수 없습니다."
            return False

        payload = {"scheduled_at": format_kst(selected_dt)}
        status_code, body = self._request_http(server, "POST", "/api/reservations", payload, secret)
        if status_code in (200, 201):
            self.warning_visible = False
            self.confirmation_visible = True
            self.confirmation_message = "예약이 등록되었습니다"
            self.confirmation_dialog_shown = True
            self.dialog_message = "예약이 등록되었습니다"
            return True
        self.warning_visible = True
        self.warning_message = body.get("error", "예약 생성 실패")
        self.confirmation_visible = False
        return False

    def _request_http(
        self, server: MiniPCServer, method: str, path: str, payload: dict[str, Any], secret: str
    ) -> tuple[int, dict[str, Any]]:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Content-Length": str(len(body_bytes)),
            "X-Shared-Secret": secret,
            "Authorization": f"Bearer {secret}",
        }
        lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"] + [f"{k}: {v}" for k, v in headers.items()]
        raw_req = "\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body_bytes
        sock = MockSocket(raw_req)
        MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
        raw_resp = sock.wfile.getvalue().decode("utf-8")
        status_line, rest = raw_resp.split("\r\n", 1)
        body = rest.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in rest else ""
        return int(status_line.split(" ")[1]), (json.loads(body) if body else {})


class TestAndroidCreateReservationLayoutAndResources(unittest.TestCase):
    """레이아웃, 매니페스트 및 리소스 무결성 검증 (LB-013, D-03, REQ-028, REQ-029)."""

    def test_file_line_counts_under_300(self) -> None:
        """모든 관련 파일이 300줄 이하인지 확인."""
        checked = [
            RES_LAYOUT / "activity_create_reservation.xml",
            RES_LAYOUT / "activity_main.xml",
            RES_VALUES / "strings.xml",
            MANIFEST_PATH,
            SRC_JAVA / "CreateReservationActivity.kt",
            SRC_JAVA / "MainActivity.kt",
            SRC_JAVA / "network/ServerClient.kt",
            SRC_JAVA / "model/CreateReservationResult.kt",
        ]
        for p in checked:
            self.assertTrue(p.is_file(), f"파일 누락: {p}")
            with open(p, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLessEqual(lines, 300, f"{p.name} 줄 수 초과 ({lines} > 300)")

    def test_create_reservation_layout_elements(self) -> None:
        """activity_create_reservation.xml 필수 뷰 ID 및 구조 검증."""
        tree = ET.parse(RES_LAYOUT / "activity_create_reservation.xml")
        ns = "http://schemas.android.com/apk/res/android"
        ids = {e.attrib.get(f"{{{ns}}}id", "").split("/")[-1] for e in tree.iter()}

        required_ids = [
            "textTitle", "cardNoticeModeZone", "textNoticeModeZone", "cardDateTimeSelection",
            "btnPickDate", "textSelectedDate", "btnPickTime", "textSelectedTime",
            "cardSelectedPreview", "textSelectedDateTime", "textIsoPreview",
            "cardValidationWarning", "textValidationMessage", "cardConfirmation",
            "textConfirmationMessage", "btnSaveReservation", "progressBarSave", "btnCancel"
        ]
        for req_id in required_ids:
            self.assertIn(req_id, ids, f"activity_create_reservation.xml에 '{req_id}' 누락")

    def test_d03_no_mode_or_zone_selection_in_layout(self) -> None:
        """D-03 검증: 레이아웃에 모드·구역 선택 UI가 존재하지 않음."""
        tree = ET.parse(RES_LAYOUT / "activity_create_reservation.xml")
        tag_names = [e.tag.split("}")[-1] for e in tree.iter()]
        self.assertNotIn("Spinner", tag_names)
        self.assertNotIn("RadioGroup", tag_names)

        ns = "http://schemas.android.com/apk/res/android"
        for e in tree.iter():
            view_id = e.attrib.get(f"{{{ns}}}id", "").lower()
            for forbidden in ("mode", "zone", "room", "area"):
                if "noticemodezone" in view_id:
                    continue
                self.assertNotIn(forbidden, view_id, f"D-03 위반: 모드/구역 관련 뷰 ID 감지 '{view_id}'")

    def test_main_activity_has_create_reservation_button(self) -> None:
        """activity_main.xml에 '예약 추가' 버튼(btnCreateReservation) 존재 확인 (SPEC F-1)."""
        tree = ET.parse(RES_LAYOUT / "activity_main.xml")
        ns = "http://schemas.android.com/apk/res/android"
        ids = {e.attrib.get(f"{{{ns}}}id", "").split("/")[-1] for e in tree.iter()}
        self.assertIn("btnCreateReservation", ids)

    def test_manifest_activity_declared_with_responsive_config(self) -> None:
        """AndroidManifest.xml에 CreateReservationActivity 선언 및 폴더블 설정 검증 (REQ-028, REQ-029)."""
        tree = ET.parse(MANIFEST_PATH)
        ns = "http://schemas.android.com/apk/res/android"
        activities = [e for e in tree.iter("activity")]
        names = [a.attrib.get(f"{{{ns}}}name") for a in activities]
        self.assertIn(".CreateReservationActivity", names)

        target = next(a for a in activities if a.attrib.get(f"{{{ns}}}name") == ".CreateReservationActivity")
        self.assertEqual(target.attrib.get(f"{{{ns}}}exported"), "false")
        config = target.attrib.get(f"{{{ns}}}configChanges", "")
        self.assertTrue(all(k in config for k in ("screenSize", "smallestScreenSize", "screenLayout")))

    def test_strings_exact_messages(self) -> None:
        """strings.xml 필수 메시지 검증 (done_when 문구)."""
        tree = ET.parse(RES_VALUES / "strings.xml")
        strings = {e.attrib["name"]: (e.text or "").strip() for e in tree.findall("string")}

        self.assertEqual(strings.get("btn_add_reservation"), "예약 추가")
        self.assertEqual(strings.get("btn_save_reservation"), "예약 저장")
        self.assertIn("예약이 등록되었습니다", strings.get("msg_confirm_reservation_created", ""))
        self.assertIn("과거 시각은 예약할 수 없습니다", strings.get("msg_error_past_datetime", ""))
        self.assertIn("D-03", strings.get("notice_mode_zone_d03", ""))


class TestAndroidCreateReservationSourceContracts(unittest.TestCase):
    """코틀린 소스 코드 계약 검증."""

    def test_server_client_create_reservation_contract(self) -> None:
        client_code = (SRC_JAVA / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("fun createReservation(", client_code)
        self.assertIn("POST", client_code)
        self.assertIn("/api/reservations", client_code)
        self.assertIn("X-Shared-Secret", client_code)
        self.assertIn("scheduled_at", client_code)
        for forbidden in ('"mode"', '"zone"', '"room"', '"area"'):
            self.assertNotIn(forbidden, client_code)

    def test_create_reservation_activity_contract(self) -> None:
        act_code = (SRC_JAVA / "CreateReservationActivity.kt").read_text(encoding="utf-8")
        self.assertIn("class CreateReservationActivity", act_code)
        self.assertIn("checkDateTimeValidity", act_code)
        self.assertIn("formatIsoKst", act_code)
        self.assertIn("msg_error_past_datetime", act_code)
        self.assertIn("msg_confirm_reservation_created", act_code)
        self.assertIn("btnSaveReservation", act_code)


class TestAndroidCreateReservationFlowIntegration(unittest.TestCase):
    """예약 생성 전체 흐름 통합 테스트 (LB-013 done_when, REQ-001, D-03)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_res.db"
        self.secret = "shared-secret-lb013"
        self.storage = Storage(self.db_path)
        self.config = Config(host="127.0.0.1", port=0, shared_secret=self.secret, db_path=self.db_path)
        self.server = MiniPCServer(self.config, self.storage)
        self.curr_now = now_kst().replace(second=0, microsecond=0)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.temp_dir.cleanup()

    def test_done_when_future_time_creates_reservation_and_shows_confirmation(self) -> None:
        """done_when: 앱에서 날짜·시각을 골라 저장하면 확인 메시지가 뜨고."""
        ui = SimulatedCreateReservationUiState()
        future_dt = self.curr_now + timedelta(hours=2)

        ui.on_date_time_changed(future_dt, self.curr_now)
        self.assertFalse(ui.warning_visible)

        saved = ui.on_save_clicked(future_dt, self.curr_now, self.server, "http://127.0.0.1:8080", self.secret)
        self.assertTrue(saved)
        self.assertTrue(ui.server_request_sent)

        self.assertTrue(ui.confirmation_visible)
        self.assertEqual(ui.confirmation_message, "예약이 등록되었습니다")
        self.assertTrue(ui.confirmation_dialog_shown)
        self.assertEqual(ui.dialog_message, "예약이 등록되었습니다")
        self.assertFalse(ui.warning_visible)

        reservations = self.storage.list_reservations()
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0].status, "예정")
        self.assertEqual(reservations[0].reserved_at, future_dt)

    def test_done_when_past_time_rejects_saving_and_shows_guidance(self) -> None:
        """done_when: 과거 시각을 고르면 저장되지 않고 안내가 보인다."""
        ui = SimulatedCreateReservationUiState()
        past_dt = self.curr_now - timedelta(hours=2)

        ui.on_date_time_changed(past_dt, self.curr_now)
        self.assertTrue(ui.warning_visible)
        self.assertEqual(ui.warning_message, "과거 시각은 예약할 수 없습니다.")

        saved = ui.on_save_clicked(past_dt, self.curr_now, self.server, "http://127.0.0.1:8080", self.secret)
        self.assertFalse(saved)
        self.assertFalse(ui.server_request_sent)
        self.assertTrue(ui.warning_visible)
        self.assertEqual(ui.warning_message, "과거 시각은 예약할 수 없습니다.")
        self.assertFalse(ui.confirmation_visible)
        self.assertFalse(ui.confirmation_dialog_shown)

        reservations = self.storage.list_reservations()
        self.assertEqual(len(reservations), 0)

    def test_current_exact_time_also_rejected(self) -> None:
        """현재 시각과 동일한 경우도 미래가 아니므로 저장 불가 및 안내 표시."""
        ui = SimulatedCreateReservationUiState()
        saved = ui.on_save_clicked(self.curr_now, self.curr_now, self.server, "http://127.0.0.1:8080", self.secret)
        self.assertFalse(saved)
        self.assertFalse(ui.server_request_sent)
        self.assertTrue(ui.warning_visible)
        self.assertEqual(ui.warning_message, "과거 시각은 예약할 수 없습니다.")


if __name__ == "__main__":
    unittest.main()
