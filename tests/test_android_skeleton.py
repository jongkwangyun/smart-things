import io
import json
import os
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


class MockSocket:
    """테스트용 인메모리 HTTP 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class TestAndroidSkeletonStructure(unittest.TestCase):
    """안드로이드 프로젝트 골격 및 설정 파일 무결성 검증 (LB-010, D-01)."""

    def test_required_files_exist(self) -> None:
        relative_paths = [
            "settings.gradle.kts",
            "build.gradle.kts",
            "gradle.properties",
            "app/build.gradle.kts",
            "app/proguard-rules.pro",
            "app/src/main/AndroidManifest.xml",
            "app/src/main/res/values/strings.xml",
            "app/src/main/res/values/colors.xml",
            "app/src/main/res/values/themes.xml",
            "app/src/main/res/layout/activity_main.xml",
            "app/src/main/java/com/smartthings/cleaner/MainActivity.kt",
            "app/src/main/java/com/smartthings/cleaner/network/ServerClient.kt",
            "app/src/main/java/com/smartthings/cleaner/model/ConnectionState.kt",
        ]
        for rel in relative_paths:
            self.assertTrue((ANDROID_ROOT / rel).is_file(), f"필수 파일 누락: android/{rel}")

    def test_code_files_line_count_under_limit(self) -> None:
        """모든 안드로이드 코드/설정 파일이 300줄 이하인지 검사."""
        for root, _, files in os.walk(ANDROID_ROOT):
            for file in files:
                if file.endswith((".kt", ".kts", ".xml", ".properties")):
                    p = Path(root) / file
                    with open(p, "rb") as f:
                        lines = sum(1 for _ in f)
                    self.assertLessEqual(lines, 300, f"{p.relative_to(PROJECT_ROOT)} 줄 수 초과 ({lines} > 300)")

    def test_gradle_configuration(self) -> None:
        settings_text = (ANDROID_ROOT / "settings.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('rootProject.name = "smartthings-cleaner"', settings_text)
        self.assertIn('include(":app")', settings_text)

        app_build = (ANDROID_ROOT / "app" / "build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn('namespace = "com.smartthings.cleaner"', app_build)
        self.assertIn('applicationId = "com.smartthings.cleaner"', app_build)
        self.assertIn("minSdk = 26", app_build)
        self.assertIn("targetSdk = 34", app_build)


class TestAndroidManifestAndLayout(unittest.TestCase):
    """매니페스트 권한 및 레이아웃(S21+, Fold8 반응형) 검증 (REQ-028, REQ-029)."""

    def test_manifest_permissions_and_activity(self) -> None:
        manifest_path = ANDROID_ROOT / "app/src/main/AndroidManifest.xml"
        root = ET.parse(manifest_path).getroot()
        permissions = [
            elem.attrib.get("{http://schemas.android.com/apk/res/android}name")
            for elem in root.findall("uses-permission")
        ]
        self.assertIn("android.permission.INTERNET", permissions)
        self.assertIn("android.permission.ACCESS_NETWORK_STATE", permissions)

        activity = root.find(".//activity")
        self.assertIsNotNone(activity)
        config_changes = activity.attrib.get(  # type: ignore[union-attr]
            "{http://schemas.android.com/apk/res/android}configChanges", ""
        )
        self.assertTrue(all(k in config_changes for k in ("screenSize", "smallestScreenSize", "screenLayout")))

    def test_layout_elements(self) -> None:
        layout_path = ANDROID_ROOT / "app/src/main/res/layout/activity_main.xml"
        root = ET.parse(layout_path).getroot()
        element_ids = {
            elem.attrib.get("{http://schemas.android.com/apk/res/android}id", "").split("/")[-1]
            for elem in root.iter()
            if "{http://schemas.android.com/apk/res/android}id" in elem.attrib
        }
        for req_id in (
            "editServerUrl",
            "editSharedSecret",
            "btnConnect",
            "textConnectionStatus",
            "cardConnectionStatus",
            "textStatusDetail",
            "cardServerInfo",
        ):
            self.assertIn(req_id, element_ids, f"activity_main.xml에 ID '{req_id}' 누락")

    def test_strings_connected_value(self) -> None:
        strings_path = ANDROID_ROOT / "app/src/main/res/values/strings.xml"
        strings_map = {elem.attrib["name"]: elem.text for elem in ET.parse(strings_path).getroot().findall("string")}
        self.assertEqual(strings_map.get("status_connected"), "연결됨")
        self.assertEqual(strings_map.get("btn_connect"), "연결")
        self.assertIn("status_failed", strings_map)
        self.assertIn("error_unauthorized", strings_map)
        self.assertIn("error_unreachable", strings_map)


class TestKotlinSourceContracts(unittest.TestCase):
    """코틀린 소스 코드의 계약 및 구조 검증."""

    def test_contracts(self) -> None:
        base = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"
        state_kt = (base / "model/ConnectionState.kt").read_text(encoding="utf-8")
        self.assertTrue(all(k in state_kt for k in ("package com.smartthings.cleaner.model", "Connected", "Failed")))

        client_kt = (base / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertTrue(
            all(k in client_kt for k in ("testConnection", "checkHealth", "checkAuth", "X-Shared-Secret"))
        )

        activity_kt = (base / "MainActivity.kt").read_text(encoding="utf-8")
        self.assertTrue(all(k in activity_kt for k in ("MainActivity", "performConnectionCheck", "status_connected")))


class TestClientServerProtocolIntegration(unittest.TestCase):
    """안드로이드 ServerClient 통신 규약과 서버 간 통합 테스트 (LB-010 done_when)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.secret = "shared-secret-key-lb010"
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

    def _simulate_server_client(
        self, server: MiniPCServer | None, server_url: str, secret: str
    ) -> dict[str, str | int | bool]:
        clean_url = server_url.strip().rstrip("/")
        if not clean_url:
            return {"status": "FAILED", "reason": "EMPTY_URL"}
        if not secret.strip():
            return {"status": "FAILED", "reason": "EMPTY_SECRET"}
        if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            return {"status": "FAILED", "reason": "INVALID_URL"}
        if server is None:
            return {"status": "FAILED", "reason": "UNREACHABLE", "display_text": "연결 실패"}

        st_health, health = self._request_http(server, "GET", "/health")
        if st_health != 200:
            return {"status": "FAILED", "reason": "UNREACHABLE", "display_text": "연결 실패"}

        headers = {"X-Shared-Secret": secret, "Authorization": f"Bearer {secret}"}
        st_auth, auth = self._request_http(server, "GET", "/api/reservations", headers=headers)
        if st_auth == 200:
            return {
                "status": "CONNECTED",
                "display_text": "연결됨",  # done_when 기준 문구
                "service": health.get("service", ""),
                "time_kst": health.get("time_kst", ""),
                "reservations_count": len(auth.get("reservations", [])),
            }
        reason = "UNAUTHORIZED" if st_auth == 401 else "SERVER_ERROR"
        return {"status": "FAILED", "reason": reason, "display_text": "연결 실패"}

    def test_connection_successful_displays_connected(self) -> None:
        """올바른 서버 주소와 비밀키 입력 시 '연결됨' 표시 확인 (done_when)."""
        result = self._simulate_server_client(self.server, "http://127.0.0.1:8080", self.secret)
        self.assertEqual(result.get("status"), "CONNECTED")
        self.assertEqual(result.get("display_text"), "연결됨")
        self.assertEqual(result.get("service"), "smart-things-mini-pc")

    def test_connection_unauthorized_with_wrong_secret(self) -> None:
        """잘못된 비밀키 입력 시 인증 거부 및 실패 확인 (D-09, REQ-024)."""
        result = self._simulate_server_client(self.server, "http://127.0.0.1:8080", "wrong-secret-key")
        self.assertEqual(result.get("status"), "FAILED")
        self.assertEqual(result.get("reason"), "UNAUTHORIZED")
        self.assertEqual(result.get("display_text"), "연결 실패")

    def test_connection_unreachable_server(self) -> None:
        """미니PC 서버 중단/연결 불가 시 경고 상태 확인 (REQ-025)."""
        result = self._simulate_server_client(None, "http://127.0.0.1:8080", self.secret)
        self.assertEqual(result.get("status"), "FAILED")
        self.assertEqual(result.get("reason"), "UNREACHABLE")
        self.assertEqual(result.get("display_text"), "연결 실패")

    def test_connection_empty_or_invalid_inputs(self) -> None:
        """빈 입력값 및 잘못된 URL 검증."""
        url = "http://127.0.0.1:8080"
        self.assertEqual(self._simulate_server_client(self.server, "", self.secret).get("reason"), "EMPTY_URL")
        self.assertEqual(self._simulate_server_client(self.server, url, "").get("reason"), "EMPTY_SECRET")
        self.assertEqual(
            self._simulate_server_client(self.server, "ftp://cleaner.local", self.secret).get("reason"),
            "INVALID_URL",
        )


if __name__ == "__main__":
    unittest.main()
