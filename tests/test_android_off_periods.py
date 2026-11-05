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


class SimulatedCreateOffPeriodUiState:
    """CreateOffPeriodActivity UI 상태 전이 시뮬레이터 (LB-020, SPEC F-3)."""

    def __init__(self) -> None:
        self.warning_visible: bool = False
        self.warning_message: str = ""
        self.confirmation_visible: bool = False
        self.confirmation_message: str = ""
        self.confirmation_dialog_shown: bool = False
        self.dialog_message: str = ""
        self.server_request_sent: bool = False

    def on_date_time_changed(self, start_dt: datetime, end_dt: datetime) -> None:
        if end_dt <= start_dt:
            self.warning_visible = True
            self.warning_message = "종료 일시는 시작 일시보다 뒤여야 합니다."
            self.confirmation_visible = False
        else:
            self.warning_visible = False

    def on_save_clicked(
        self, start_dt: datetime, end_dt: datetime, server: MiniPCServer | None, server_url: str, secret: str
    ) -> bool:
        if end_dt <= start_dt:
            self.warning_visible = True
            self.warning_message = "종료 일시는 시작 일시보다 뒤여야 합니다."
            self.confirmation_visible = False
            self.server_request_sent = False
            return False

        self.server_request_sent = True
        clean_url = server_url.strip().rstrip("/")
        if not clean_url or not secret.strip() or server is None:
            self.warning_visible = True
            self.warning_message = "서버에 연결할 수 없습니다."
            return False

        payload = {"start_at": format_kst(start_dt), "end_at": format_kst(end_dt)}
        status_code, body = self._request_http(server, "POST", "/api/off-periods", payload, secret)
        if status_code in (200, 201):
            self.warning_visible = False
            self.confirmation_visible = True
            self.confirmation_message = "쉬는 기간이 등록되었습니다"
            self.confirmation_dialog_shown = True
            self.dialog_message = "쉬는 기간이 등록되었습니다"
            return True
        self.warning_visible = True
        self.warning_message = body.get("error", "쉬는 기간 등록 실패")
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


class TestAndroidCreateOffPeriodLayoutAndResources(unittest.TestCase):
    """쉬는 기간 등록 레이아웃 및 리소스 무결성 검증 (LB-020, REQ-011, REQ-028, REQ-029)."""

    def test_file_line_counts_under_300(self) -> None:
        """모든 관련 파일이 300줄 이하인지 확인."""
        checked = [
            RES_LAYOUT / "activity_create_off_period.xml",
            RES_LAYOUT / "activity_main.xml",
            RES_VALUES / "strings.xml",
            MANIFEST_PATH,
            SRC_JAVA / "CreateOffPeriodActivity.kt",
            SRC_JAVA / "MainActivity.kt",
            SRC_JAVA / "network/ServerClient.kt",
            SRC_JAVA / "model/CreateOffPeriodResult.kt",
            PROJECT_ROOT / "server/server.py",
            PROJECT_ROOT / "server/off_periods/validator.py",
            PROJECT_ROOT / "server/off_periods/service.py",
        ]
        for p in checked:
            self.assertTrue(p.is_file(), f"파일 누락: {p}")
            with open(p, encoding="utf-8") as f:
                lines = sum(1 for _ in f)
            self.assertLessEqual(lines, 300, f"{p.name} 줄 수 초과 ({lines} > 300)")

    def test_create_off_period_layout_elements(self) -> None:
        """activity_create_off_period.xml 필수 뷰 ID 및 구조 검증."""
        tree = ET.parse(RES_LAYOUT / "activity_create_off_period.xml")
        ns = "http://schemas.android.com/apk/res/android"
        ids = {e.attrib.get(f"{{{ns}}}id", "").split("/")[-1] for e in tree.iter()}

        required_ids = [
            "textTitle", "cardStartDateSelection", "btnPickStartDate", "textSelectedStartDate",
            "btnPickStartTime", "textSelectedStartTime", "cardEndDateSelection", "btnPickEndDate",
            "textSelectedEndDate", "btnPickEndTime", "textSelectedEndTime", "cardSelectedPreview",
            "textSelectedRangePreview", "cardValidationWarning", "textValidationMessage",
            "cardConfirmation", "textConfirmationMessage", "btnSaveOffPeriod", "progressBarSave", "btnCancel"
        ]
        for req_id in required_ids:
            self.assertIn(req_id, ids, f"activity_create_off_period.xml에 '{req_id}' 누락")

    def test_main_activity_has_create_off_period_button(self) -> None:
        """쉬는 기간 등록 진입 경로 존재 확인 (SPEC F-3).

        LB-047로 메인은 연결 전용이 되고 홈이 신설되어 진입 경로가
        홈 '쉬는 기간' 타일(tileOffPeriod) -> 쉬는 기간 목록의 추가 버튼(btnAddOffPeriod)
        -> 등록 화면(CreateOffPeriodActivity)으로 이동했다. 요구(등록 진입 가능)는 유지하고 대상만 갱신한다.
        """
        ns = "http://schemas.android.com/apk/res/android"

        def layout_ids(name: str) -> set[str]:
            tree = ET.parse(RES_LAYOUT / name)
            return {e.attrib.get(f"{{{ns}}}id", "").split("/")[-1] for e in tree.iter()}

        self.assertIn("tileOffPeriod", layout_ids("activity_home.xml"))
        self.assertIn("btnAddOffPeriod", layout_ids("activity_off_period_list.xml"))
        src = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"
        home = (src / "HomeActivity.kt").read_text(encoding="utf-8")
        listing = (src / "OffPeriodListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("R.id.tileOffPeriod", home)
        self.assertIn("OffPeriodListActivity::class.java", home)
        self.assertIn("CreateOffPeriodActivity::class.java", listing)

    def test_manifest_activity_declared_with_responsive_config(self) -> None:
        """AndroidManifest.xml에 CreateOffPeriodActivity 등록 및 폴더블 설정 검증 (REQ-028, REQ-029)."""
        tree = ET.parse(MANIFEST_PATH)
        ns = "http://schemas.android.com/apk/res/android"
        activities = [e for e in tree.iter("activity")]
        names = [a.attrib.get(f"{{{ns}}}name") for a in activities]
        self.assertIn(".CreateOffPeriodActivity", names)

        target = next(a for a in activities if a.attrib.get(f"{{{ns}}}name") == ".CreateOffPeriodActivity")
        self.assertEqual(target.attrib.get(f"{{{ns}}}exported"), "false")
        config = target.attrib.get(f"{{{ns}}}configChanges", "")
        self.assertTrue(all(k in config for k in ("screenSize", "smallestScreenSize", "screenLayout")))

    def test_strings_exact_messages(self) -> None:
        """strings.xml 필수 메시지 검증 (done_when 문구)."""
        tree = ET.parse(RES_VALUES / "strings.xml")
        strings = {e.attrib["name"]: (e.text or "").strip() for e in tree.findall("string")}

        self.assertEqual(strings.get("btn_create_off_period"), "쉬는 기간 등록")
        self.assertEqual(strings.get("btn_save_off_period"), "쉬는 기간 저장")
        self.assertEqual(strings.get("msg_confirm_off_period_created"), "쉬는 기간이 등록되었습니다")
        self.assertEqual(strings.get("msg_error_invalid_off_period_range"), "종료 일시는 시작 일시보다 뒤여야 합니다.")


    def test_kotlin_contracts(self) -> None:
        client_code = (SRC_JAVA / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("fun createOffPeriod(", client_code)
        self.assertIn("/api/off-periods", client_code)

        act_code = (SRC_JAVA / "CreateOffPeriodActivity.kt").read_text(encoding="utf-8")
        self.assertIn("class CreateOffPeriodActivity", act_code)
        self.assertIn("checkDateTimeValidity", act_code)
        self.assertIn("btnSaveOffPeriod", act_code)


class TestAndroidCreateOffPeriodFlowIntegration(unittest.TestCase):
    """쉬는 기간 등록 전체 흐름 통합 테스트 (LB-020 done_when, REQ-011, SPEC F-3)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_off_flow.db"
        self.secret = "shared-secret-lb020"
        self.storage = Storage(self.db_path)
        self.config = Config(host="127.0.0.1", port=0, shared_secret=self.secret, db_path=self.db_path)
        self.server = MiniPCServer(self.config, self.storage)
        self.curr_now = now_kst().replace(second=0, microsecond=0)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.temp_dir.cleanup()

    def test_done_when_valid_range_creates_off_period_and_shows_confirmation(self) -> None:
        """done_when: 앱에서 시작·종료 일시를 골라 저장하면 확인 메시지가 뜨고."""
        ui = SimulatedCreateOffPeriodUiState()
        start_dt = self.curr_now + timedelta(days=1)
        end_dt = self.curr_now + timedelta(days=3)

        ui.on_date_time_changed(start_dt, end_dt)
        self.assertFalse(ui.warning_visible)

        saved = ui.on_save_clicked(start_dt, end_dt, self.server, "http://127.0.0.1:8080", self.secret)
        self.assertTrue(saved)
        self.assertTrue(ui.server_request_sent)

        self.assertTrue(ui.confirmation_visible)
        self.assertEqual(ui.confirmation_message, "쉬는 기간이 등록되었습니다")
        self.assertTrue(ui.confirmation_dialog_shown)
        self.assertEqual(ui.dialog_message, "쉬는 기간이 등록되었습니다")
        self.assertFalse(ui.warning_visible)

        off_periods = self.storage.list_off_periods()
        self.assertEqual(len(off_periods), 1)
        self.assertEqual(off_periods[0].start_at, start_dt)
        self.assertEqual(off_periods[0].end_at, end_dt)

    def test_done_when_end_before_start_rejects_saving_and_shows_guidance(self) -> None:
        """done_when: 종료가 시작보다 앞서면 저장되지 않고 안내가 보인다."""
        ui = SimulatedCreateOffPeriodUiState()
        start_dt = self.curr_now + timedelta(days=3)
        end_dt = self.curr_now + timedelta(days=1)

        ui.on_date_time_changed(start_dt, end_dt)
        self.assertTrue(ui.warning_visible)
        self.assertEqual(ui.warning_message, "종료 일시는 시작 일시보다 뒤여야 합니다.")

        saved = ui.on_save_clicked(start_dt, end_dt, self.server, "http://127.0.0.1:8080", self.secret)
        self.assertFalse(saved)
        self.assertFalse(ui.server_request_sent)
        self.assertTrue(ui.warning_visible)
        self.assertEqual(ui.warning_message, "종료 일시는 시작 일시보다 뒤여야 합니다.")
        self.assertFalse(ui.confirmation_visible)
        self.assertFalse(ui.confirmation_dialog_shown)

        off_periods = self.storage.list_off_periods()
        self.assertEqual(len(off_periods), 0)

    def test_done_when_end_equals_start_also_rejects_saving_and_shows_guidance(self) -> None:
        """종료와 시작이 같은 시각인 경우에도 저장되지 않고 안내 표시."""
        ui = SimulatedCreateOffPeriodUiState()
        start_dt = self.curr_now + timedelta(days=1)
        end_dt = start_dt

        saved = ui.on_save_clicked(start_dt, end_dt, self.server, "http://127.0.0.1:8080", self.secret)
        self.assertFalse(saved)
        self.assertFalse(ui.server_request_sent)
        self.assertTrue(ui.warning_visible)
        self.assertEqual(ui.warning_message, "종료 일시는 시작 일시보다 뒤여야 합니다.")

        off_periods = self.storage.list_off_periods()
        self.assertEqual(len(off_periods), 0)


if __name__ == "__main__":
    unittest.main()
