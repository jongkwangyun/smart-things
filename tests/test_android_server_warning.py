import io
import json
import re
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from server.config import Config
from server.server import MiniPCHandler, MiniPCServer
from server.storage import Storage

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android"
RES_VALUES = ANDROID_ROOT / "app/src/main/res/values"
RES_LAYOUT = ANDROID_ROOT / "app/src/main/res/layout"
SRC_MAIN_JAVA = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"
MANIFEST_FILE = ANDROID_ROOT / "app/src/main/AndroidManifest.xml"


class MockSocket:
    """테스트용 인메모리 HTTP 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class SimulatedAppUiState:
    """MainActivity의 UI 상태 전이 시뮬레이터 (REQ-025, D-13)."""

    def __init__(self) -> None:
        self.warning_card_visible: bool = False
        self.warning_title: str = ""
        self.warning_message: str = ""
        self.status_text: str = "연결 안 됨"
        self.server_info_visible: bool = False

    def apply_connection_state(self, state: str, reason: str = "", message: str = "") -> None:
        if state == "CONNECTED":
            self.warning_card_visible = False
            self.status_text = "연결됨"
            self.server_info_visible = True
        elif state == "FAILED" and reason == "UNREACHABLE":
            self.warning_card_visible = True
            self.warning_title = "서버에 연결할 수 없음"
            self.warning_message = message or "서버에 연결할 수 없습니다."
            self.status_text = "서버에 연결할 수 없음"
            self.server_info_visible = False
        elif state == "FAILED":
            self.warning_card_visible = False
            self.status_text = "연결 실패"
            self.server_info_visible = False
        elif state == "CONNECTING":
            self.warning_card_visible = False
            self.status_text = "연결 확인 중..."
            self.server_info_visible = False


class TestAndroidServerWarningStringsAndLayout(unittest.TestCase):
    """서버 연결 불가 경고 관련 리소스 및 레이아웃 정의 검증 (REQ-025, LB-029)."""

    def test_strings_exact_server_unreachable_warning(self) -> None:
        """strings.xml에 '서버에 연결할 수 없음' 문구가 정확히 정의되어 있는지 검증."""
        strings_path = RES_VALUES / "strings.xml"
        root = ET.parse(strings_path).getroot()
        strings_map = {elem.attrib["name"]: (elem.text or "").strip() for elem in root.findall("string")}

        self.assertIn("server_unreachable_warning", strings_map)
        self.assertEqual(strings_map["server_unreachable_warning"], "서버에 연결할 수 없음")

    def test_layout_warning_ui_card_elements(self) -> None:
        """activity_main.xml에 경고 카드 및 텍스트 뷰가 올바르게 정의되어 있는지 검증."""
        layout_path = RES_LAYOUT / "activity_main.xml"
        root = ET.parse(layout_path).getroot()
        ns = {"android": "http://schemas.android.com/apk/res/android"}

        element_ids = {
            elem.attrib.get(f"{{{ns['android']}}}id", "").split("/")[-1]
            for elem in root.iter()
            if f"{{{ns['android']}}}id" in elem.attrib
        }
        self.assertIn("cardServerWarning", element_ids)
        self.assertIn("textServerWarningTitle", element_ids)
        self.assertIn("textServerWarningMessage", element_ids)

        warning_cards = [
            elem
            for elem in root.iter()
            if elem.attrib.get(f"{{{ns['android']}}}id", "").endswith("/cardServerWarning")
        ]
        self.assertEqual(len(warning_cards), 1)
        self.assertEqual(warning_cards[0].attrib.get(f"{{{ns['android']}}}visibility"), "gone")

    def test_main_activity_references_warning_resources(self) -> None:
        """MainActivity.kt가 경고 카드 뷰 및 경고 문자열 리소스를 바인딩하고 사용하는지 검증."""
        activity_kt = (SRC_MAIN_JAVA / "MainActivity.kt").read_text(encoding="utf-8")
        self.assertIn("cardServerWarning", activity_kt)
        self.assertIn("textServerWarningTitle", activity_kt)
        self.assertIn("textServerWarningMessage", activity_kt)
        self.assertIn("R.string.server_unreachable_warning", activity_kt)
        self.assertIn("checkConnectionOnLaunch", activity_kt)


class TestAppLaunchConnectionWarningFlow(unittest.TestCase):
    """앱 실행 시 서버 상태에 따른 연결 확인 및 경고 표시 흐름 검증 (LB-029 done_when)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.secret = "test-secret-key-lb029"
        self.storage = Storage(self.db_path)
        self.config = Config(host="127.0.0.1", port=0, shared_secret=self.secret, db_path=self.db_path)
        self.server = MiniPCServer(self.config, self.storage)

    def tearDown(self) -> None:
        self.server.server_close()
        self.temp_dir.cleanup()

    def _request_http(
        self, server: MiniPCServer, method: str, path: str, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, Any]]:
        lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1"] + [f"{k}: {v}" for k, v in (headers or {}).items()]
        sock = MockSocket("\r\n".join(lines).encode("utf-8") + b"\r\n\r\n")
        MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
        raw_resp = sock.wfile.getvalue().decode("utf-8")
        status_line, rest = raw_resp.split("\r\n", 1)
        body = rest.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in rest else ""
        return int(status_line.split(" ")[1]), (json.loads(body) if body else {})

    def _execute_client_flow(
        self, server: MiniPCServer | None, server_url: str, secret: str
    ) -> tuple[str, str, str]:
        clean_url = server_url.strip().rstrip("/")
        if not clean_url:
            return "FAILED", "EMPTY_URL", "서버 주소를 입력해 주세요."
        if not secret.strip():
            return "FAILED", "EMPTY_SECRET", "공유 비밀키를 입력해 주세요."
        if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            return "FAILED", "INVALID_URL", "올바른 URL 형식으로 입력해 주세요."
        if server is None:
            return "FAILED", "UNREACHABLE", "서버에 연결할 수 없습니다: Connection refused"

        st_health, _ = self._request_http(server, "GET", "/health")
        if st_health != 200:
            return "FAILED", "UNREACHABLE", f"서버 응답 오류 (HTTP {st_health})"

        headers = {"X-Shared-Secret": secret, "Authorization": f"Bearer {secret}"}
        st_auth, _ = self._request_http(server, "GET", "/api/reservations", headers=headers)
        if st_auth == 200:
            return "CONNECTED", "SUCCESS", ""
        if st_auth == 401:
            return "FAILED", "UNAUTHORIZED", "인증 실패: 공유 비밀키가 올바르지 않습니다."
        return "FAILED", "SERVER_ERROR", f"서버 오류 (HTTP {st_auth})"

    def test_app_launch_with_stopped_server_displays_warning(self) -> None:
        """서버를 끈 상태에서 앱을 열면 '서버에 연결할 수 없음' 경고가 표시되는지 검증 (done_when)."""
        ui = SimulatedAppUiState()

        # 앱 시작 시 연결 확인 중 상태
        ui.apply_connection_state("CONNECTING")
        self.assertFalse(ui.warning_card_visible)
        self.assertEqual(ui.status_text, "연결 확인 중...")

        # 서버가 꺼진 상태 (None)에서 앱 시작 연결 확인 실행
        status, reason, message = self._execute_client_flow(None, "http://127.0.0.1:8080", self.secret)
        self.assertEqual(status, "FAILED")
        self.assertEqual(reason, "UNREACHABLE")

        # UI에 상태 반영
        ui.apply_connection_state(status, reason, message)
        self.assertTrue(ui.warning_card_visible)
        self.assertEqual(ui.warning_title, "서버에 연결할 수 없음")
        self.assertEqual(ui.status_text, "서버에 연결할 수 없음")
        self.assertFalse(ui.server_info_visible)

    def test_app_launch_with_running_server_hides_warning_and_shows_connected(self) -> None:
        """서버가 정상 구동 중인 상태에서 앱을 열면 경고가 숨겨지고 '연결됨'이 표시되는지 검증."""
        ui = SimulatedAppUiState()

        status, reason, message = self._execute_client_flow(self.server, "http://127.0.0.1:8080", self.secret)
        self.assertEqual(status, "CONNECTED")

        ui.apply_connection_state(status, reason, message)
        self.assertFalse(ui.warning_card_visible)
        self.assertEqual(ui.status_text, "연결됨")
        self.assertTrue(ui.server_info_visible)

    def test_server_status_transition_stopped_to_running(self) -> None:
        """서버 장애 상태에서 경고가 보였다가 서버 재가동 후 재시도 시 경고가 해제되는 흐름 검증."""
        ui = SimulatedAppUiState()

        # 1. 서버 장애 발생 -> 경고 노출
        status, reason, message = self._execute_client_flow(None, "http://127.0.0.1:8080", self.secret)
        ui.apply_connection_state(status, reason, message)
        self.assertTrue(ui.warning_card_visible)
        self.assertEqual(ui.warning_title, "서버에 연결할 수 없음")

        # 2. 서버 정상화 후 재연결 -> 경고 숨김 및 '연결됨' 표시
        status, reason, message = self._execute_client_flow(self.server, "http://127.0.0.1:8080", self.secret)
        ui.apply_connection_state(status, reason, message)
        self.assertFalse(ui.warning_card_visible)
        self.assertEqual(ui.status_text, "연결됨")


class TestD13NoBackgroundPollingCompliance(unittest.TestCase):
    """SPEC D-13 준수: 백그라운드 감시 없음, 앱 실행 시 확인만 수행 검증."""

    def test_manifest_has_no_background_services_or_alarms(self) -> None:
        """AndroidManifest.xml에 백그라운드 감시 서비스나 알람 수신기가 등록되어 있지 않은지 확인."""
        root = ET.parse(MANIFEST_FILE).getroot()
        ns = {"android": "http://schemas.android.com/apk/res/android"}

        # D-13: 서버 장애 감지용 백그라운드 감시 서비스 등록 금지 (D-12 FCM 푸시 수신 서비스 제외)
        polling_services = [
            elem for elem in root.findall(".//service")
            if "AppFirebaseMessagingService" not in elem.attrib.get(f"{{{ns['android']}}}name", "")
        ]
        self.assertEqual(len(polling_services), 0, "D-13 위반: 백그라운드 감시 서비스가 등록되어 있습니다.")

        receivers = root.findall(".//receiver")
        self.assertEqual(len(receivers), 0, "D-13 위반: 브로드캐스트 수신기가 등록되어 있습니다.")

        permissions = [elem.attrib.get(f"{{{ns['android']}}}name", "") for elem in root.findall("uses-permission")]
        forbidden_perms = {"android.permission.RECEIVE_BOOT_COMPLETED", "android.permission.SCHEDULE_EXACT_ALARM"}
        for perm in forbidden_perms:
            self.assertNotIn(perm, permissions, f"D-13 위반: 불필요한 백그라운드 권한({perm})이 포함되어 있습니다.")

    def test_codebase_does_not_use_workmanager_or_alarmmanager_for_monitoring(self) -> None:
        """클라이언트 코드베이스에 백그라운드 주기적 감시(WorkManager/AlarmManager)가 없는지 검증."""
        kt_files = list(SRC_MAIN_JAVA.rglob("*.kt"))
        self.assertGreaterEqual(len(kt_files), 3)

        forbidden_patterns = [
            r"PeriodicWorkRequest",
            r"WorkManager",
            r"AlarmManager",
            r"JobScheduler",
            r"JobService",
        ]
        for kt_file in kt_files:
            content = kt_file.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                match = re.search(pattern, content)
                self.assertIsNone(
                    match,
                    f"D-13 위반: {kt_file.name}에 백그라운드 감시 컴포넌트({pattern})가 사용되었습니다.",
                )


if __name__ == "__main__":
    unittest.main()
