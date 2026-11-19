from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from server.config import Config
from server.notify import FCMClient, NotificationService
from server.off_periods.auto_stop import DEFAULT_DEVICE_ID
from server.off_periods.dnd_sync import DndSyncManager
from server.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer
from server.smartthings.auth import SmartThingsTokenManager
from server.smartthings.client import SmartThingsApiError, SmartThingsAuthError, SmartThingsClient
from server.storage import Storage
from server.time_utils import KST, format_kst, now_kst

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANDROID_ROOT = PROJECT_ROOT / "android"
SRC_JAVA = ANDROID_ROOT / "app/src/main/java/com/smartthings/cleaner"
RES_VALUES = ANDROID_ROOT / "app/src/main/res/values"


def _close_log(logger_name: str, path: Path) -> None:
    logger = logging.getLogger(logger_name)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == path.resolve():
            h.flush()
            h.close()
            logger.removeHandler(h)


class MockSocket:
    """테스트용 인메모리 HTTP 소켓 스트림."""

    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


class TestLB064AndroidContractsAndResources(unittest.TestCase):
    """LB-064 안드로이드 소스, 리소스, 줄 수 제한 및 구분 표시 계약 검증."""

    def test_modified_and_new_files_under_300_lines(self) -> None:
        files = [
            SRC_JAVA / "model/ConnectionState.kt",
            SRC_JAVA / "model/CreateOffPeriodResult.kt",
            SRC_JAVA / "network/ServerResponseParser.kt",
            SRC_JAVA / "network/ServerClient.kt",
            SRC_JAVA / "HomeWarningLogic.kt",
            SRC_JAVA / "HomeActivity.kt",
            SRC_JAVA / "CreateOffPeriodActivity.kt",
            RES_VALUES / "strings.xml",
            ANDROID_ROOT / "app/src/test/java/com/smartthings/cleaner/HomeWarningLogicTest.kt",
            ANDROID_ROOT / "app/src/test/java/com/smartthings/cleaner/network/ServerResponseParserTest.kt",
        ]
        for f in files:
            self.assertTrue(f.is_file(), f"필수 파일 누락: {f}")
            with f.open(encoding="utf-8") as fh:
                lines = sum(1 for _ in fh)
            self.assertLessEqual(lines, 300, f"{f.name} 줄 수 초과 ({lines} > 300)")

    def test_strings_xml_defines_smartthings_and_off_period_warning_strings(self) -> None:
        root = ET.parse(RES_VALUES / "strings.xml").getroot()
        strings = {e.attrib["name"]: (e.text or "").strip() for e in root.findall("string")}
        required = [
            "home_conn_connected_st_warning",
            "smartthings_auth_warning_title",
            "smartthings_auth_warning_msg",
            "smartthings_api_warning_title",
            "smartthings_api_warning_msg",
            "title_off_period_created_warning",
            "msg_confirm_off_period_created_with_warning",
        ]
        for key in required:
            self.assertIn(key, strings)
            self.assertTrue(strings[key], f"{key} 문자열이 비어 있습니다.")
        # 미니PC 연결 불가 문구와 스마트싱스 경고 문구가 구분되어 있어야 함
        self.assertNotEqual(strings["server_unreachable_warning"], strings["smartthings_auth_warning_title"])
        self.assertNotEqual(strings["server_unreachable_warning"], strings["smartthings_api_warning_title"])

    def test_kotlin_contracts_parse_health_and_warning(self) -> None:
        state_kt = (SRC_JAVA / "model/ConnectionState.kt").read_text(encoding="utf-8")
        self.assertIn("val smartthingsOk: Boolean = true", state_kt)
        self.assertIn("val smartthingsAuthState: String", state_kt)
        self.assertIn("val smartthingsLastError: String?", state_kt)

        off_res_kt = (SRC_JAVA / "model/CreateOffPeriodResult.kt").read_text(encoding="utf-8")
        self.assertIn("val warning: String? = null", off_res_kt)

        parser_kt = (SRC_JAVA / "network/ServerResponseParser.kt").read_text(encoding="utf-8")
        for token in ("smartthings_ok", "smartthings_auth_state", "last_success_at", "last_error", '"warning"'):
            self.assertIn(token, parser_kt)

        client_kt = (SRC_JAVA / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("ServerResponseParser.parseHealth", client_kt)
        self.assertIn("ServerResponseParser.parseCreateOffPeriodSuccess", client_kt)

        home_kt = (SRC_JAVA / "HomeActivity.kt").read_text(encoding="utf-8")
        for token in (
            "HomeWarningLogic.resolve",
            "smartthings_auth_warning_title",
            "smartthings_api_warning_title",
            "home_conn_connected_st_warning",
            "status_missed",
            "status_error",
        ):
            self.assertIn(token, home_kt)

        off_act_kt = (SRC_JAVA / "CreateOffPeriodActivity.kt").read_text(encoding="utf-8")
        for token in (
            "result.warning",
            "title_off_period_created_warning",
            "msg_confirm_off_period_created_with_warning",
            "cardValidationWarning",
        ):
            self.assertIn(token, off_act_kt)


class TestLB064ServerToAndroidProtocolIntegration(unittest.TestCase):
    """LB-064 서버(/health, POST /api/off-periods) <-> 안드로이드 경고 처리 통합 검증."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self.tmp.name)
        self.db_path = self.tmp_path / "lb064.db"
        self.sched_log = self.tmp_path / "sched.log"
        self.st_log = self.tmp_path / "st.log"
        self.env_path = self.tmp_path / ".env"
        self.token_file = self.tmp_path / "st_token.json"
        self.secret = "lb064-secret-1234"
        self.env_path.write_text("SMARTTHINGS_TOKEN=tok-init\n", encoding="utf-8")
        mgr = SmartThingsTokenManager.from_env(
            env_path=self.env_path, log_file=self.st_log, token_file=self.token_file
        )
        self.client = SmartThingsClient(token_manager=mgr, simulated=True)
        self.storage = Storage(self.db_path)
        self.fcm = FCMClient(simulated=True)
        self.notify = NotificationService(self.storage, self.fcm)
        self.dnd_m = DndSyncManager(storage=self.storage, client=self.client, device_id=DEFAULT_DEVICE_ID)
        self.sched = ReservationScheduler(
            storage=self.storage,
            client=self.client,
            device_id=DEFAULT_DEVICE_ID,
            log_file=self.sched_log,
            dnd_sync_manager=self.dnd_m,
        )
        self.server = MiniPCServer(
            Config(host="127.0.0.1", port=0, db_path=self.db_path, shared_secret=self.secret),
            storage=self.storage,
            notification_service=self.notify,
            scheduler=self.sched,
        )

    def tearDown(self) -> None:
        self.server.close()
        _close_log("server.scheduler", self.sched_log)
        _close_log("server.smartthings", self.st_log)
        self.tmp.cleanup()

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        lines = [f"{method} {path} HTTP/1.1", "Host: 127.0.0.1", f"X-Shared-Secret: {self.secret}"]
        if payload is not None:
            lines.extend(["Content-Type: application/json", f"Content-Length: {len(body_bytes)}"])
        sock = MockSocket("\r\n".join(lines).encode("utf-8") + b"\r\n\r\n" + body_bytes)
        MiniPCHandler(sock, ("127.0.0.1", 12345), self.server)  # type: ignore[arg-type]
        raw = sock.wfile.getvalue().decode("utf-8")
        status_line, rest = raw.split("\r\n", 1)
        body = rest.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in rest else ""
        return int(status_line.split(" ")[1]), (json.loads(body) if body else {})

    def _simulate_home_connection(self, reachable: bool = True) -> dict[str, Any]:
        if not reachable:
            return {
                "connected": False,
                "warning_visible": True,
                "warning_kind": "SERVER_UNREACHABLE",
                "warning_title": "서버에 연결할 수 없음",
            }
        code, health = self._request("GET", "/health")
        self.assertEqual(code, 200)
        res_code, _ = self._request("GET", "/api/reservations")
        self.assertEqual(res_code, 200)
        st_ok = bool(health.get("smartthings_ok", True))
        auth_state = health.get("smartthings_auth_state") or "unknown"
        if not st_ok:
            kind = "SMARTTHINGS_AUTH_FAILED" if auth_state == "auth_failed" else "SMARTTHINGS_API_ERROR"
            title = "스마트싱스 인증 오류" if auth_state == "auth_failed" else "스마트싱스 연동 오류"
            return {
                "connected": True,
                "warning_visible": True,
                "warning_kind": kind,
                "warning_title": title,
                "last_error": health.get("last_error"),
            }
        return {
            "connected": True,
            "warning_visible": False,
            "warning_kind": "NONE",
        }

    def test_home_warning_distinguishes_server_unreachable_vs_smartthings_auth_and_api_errors(self) -> None:
        """done_when + must: 미니PC 연결 실패와 스마트싱스 401/API 오류 경고를 구분해 표시한다."""
        # 1. 정상 상태 -> 경고 없음
        normal = self._simulate_home_connection(reachable=True)
        self.assertTrue(normal["connected"])
        self.assertFalse(normal["warning_visible"])

        # 2. 서버 연결됨 + /health smartthings_ok=false (401 인증 만료) -> 스마트싱스 인증 오류 경고
        base = datetime(2026, 11, 19, 12, 0, 0, tzinfo=KST)
        with patch.object(self.client, "get_device_status", side_effect=SmartThingsAuthError("HTTP 401 expired")):
            self.sched.step(base)
        st_auth_ui = self._simulate_home_connection(reachable=True)
        self.assertTrue(st_auth_ui["connected"])
        self.assertTrue(st_auth_ui["warning_visible"])
        self.assertEqual(st_auth_ui["warning_kind"], "SMARTTHINGS_AUTH_FAILED")
        self.assertEqual(st_auth_ui["warning_title"], "스마트싱스 인증 오류")
        self.assertIn("401", st_auth_ui["last_error"])

        # 3. 백오프(10s) 이후 복구 -> 이어서 API 오류(api_error) -> 스마트싱스 연동 오류 경고
        with patch.object(self.client, "get_device_status", return_value=object()):
            self.sched.step(base + timedelta(seconds=12))
        with patch.object(self.client, "get_device_status", side_effect=SmartThingsApiError("HTTP 503 down")):
            self.sched.step(base + timedelta(seconds=15))
        st_api_ui = self._simulate_home_connection(reachable=True)
        self.assertTrue(st_api_ui["connected"])
        self.assertTrue(st_api_ui["warning_visible"])
        self.assertEqual(st_api_ui["warning_kind"], "SMARTTHINGS_API_ERROR")
        self.assertEqual(st_api_ui["warning_title"], "스마트싱스 연동 오류")

        # 4. 미니PC 서버 자체 연결 불가 -> 미니PC 연결 실패 경고 (스마트싱스 경고와 제목/종류 구분)
        unreachable_ui = self._simulate_home_connection(reachable=False)
        self.assertFalse(unreachable_ui["connected"])
        self.assertTrue(unreachable_ui["warning_visible"])
        self.assertEqual(unreachable_ui["warning_kind"], "SERVER_UNREACHABLE")
        self.assertEqual(unreachable_ui["warning_title"], "서버에 연결할 수 없음")
        self.assertNotEqual(unreachable_ui["warning_title"], st_auth_ui["warning_title"])

        # 5. 스마트싱스 복구 -> 경고 해제
        with patch.object(self.client, "get_device_status", return_value=object()):
            self.sched.step(base + timedelta(seconds=20))
        recovered = self._simulate_home_connection(reachable=True)
        self.assertTrue(recovered["connected"])
        self.assertFalse(recovered["warning_visible"])

    def test_create_off_period_warning_field_presence_and_absence(self) -> None:
        """done_when: 쉬는 기간 등록 응답에 warning 포함 시 안내 표시, 정상 시 warning 없음."""
        now = now_kst().replace(second=0, microsecond=0)
        payload_ok = {
            "id": "op-normal",
            "start_at": format_kst(now - timedelta(minutes=10)),
            "end_at": format_kst(now + timedelta(hours=2)),
        }
        code_ok, body_ok = self._request("POST", "/api/off-periods", payload_ok)
        self.assertEqual(code_ok, 201)
        self.assertNotIn("warning", body_ok)

        self._request("DELETE", "/api/off-periods/op-normal")
        with patch.object(self.client, "get_dnd_status", side_effect=SmartThingsAuthError("401 token expired")):
            code_warn, body_warn = self._request(
                "POST",
                "/api/off-periods",
                {
                    "id": "op-warn",
                    "start_at": format_kst(now - timedelta(minutes=10)),
                    "end_at": format_kst(now + timedelta(hours=2)),
                },
            )
        self.assertEqual(code_warn, 201)
        self.assertIsNotNone(self.storage.get_off_period("op-warn"))
        self.assertIn("warning", body_warn)
        self.assertTrue(body_warn["warning"])


if __name__ == "__main__":
    unittest.main()
