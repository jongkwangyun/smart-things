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


def simulate_http_request(
    server: MiniPCServer,
    method: str,
    path: str,
    secret: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
    headers = ["Host: 127.0.0.1"]
    if secret:
        headers.append(f"X-Shared-Secret: {secret}")
        headers.append(f"Authorization: Bearer {secret}")
    if payload is not None:
        headers.append("Content-Type: application/json; charset=UTF-8")
        headers.append(f"Content-Length: {len(body_bytes)}")
    raw = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n" + body_bytes.decode("latin1")
    sock = MockSocket(raw.encode("utf-8"))
    MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
    response_bytes = sock.wfile.getvalue()
    parts = response_bytes.split(b"\r\n\r\n", 1)
    status_line = parts[0].split(b"\r\n")[0].decode("utf-8")
    status_code = int(status_line.split(" ")[1])
    body_data = json.loads(parts[1].decode("utf-8")) if len(parts) > 1 and parts[1] else {}
    return status_code, body_data


class TestAndroidOffPeriodList(unittest.TestCase):
    """LB-021: 쉬는 기간 목록 화면(OffPeriodListActivity) 및 API 단위/통합 테스트."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_off_list.db"
        self.storage = Storage(self.db_path)
        self.shared_secret = "shared-secret-ab12"
        self.config = Config(host="127.0.0.1", port=0, shared_secret=self.shared_secret, db_path=self.db_path)
        self.server = MiniPCServer(self.config, self.storage)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.temp_dir.cleanup()

    def test_file_line_counts_under_300(self) -> None:
        """Rules: 모든 관련 파일이 300줄 이하인지 확인."""
        checked_files = [
            SRC_JAVA / "model/OffPeriodItem.kt",
            SRC_JAVA / "model/FetchOffPeriodsResult.kt",
            SRC_JAVA / "network/ServerClient.kt",
            SRC_JAVA / "MainActivity.kt",
            SRC_JAVA / "OffPeriodListActivity.kt",
            RES_LAYOUT / "item_off_period.xml",
            RES_LAYOUT / "activity_off_period_list.xml",
            RES_LAYOUT / "activity_main.xml",
            RES_VALUES / "colors.xml",
            RES_VALUES / "strings.xml",
            MANIFEST_PATH,
            Path(__file__),
        ]
        for p in checked_files:
            self.assertTrue(p.is_file(), f"파일 누락: {p}")
            lines = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 300, f"{p.name} 줄 수 초과 ({lines} > 300)")

    def test_must_two_users_registered_periods_all_visible(self) -> None:
        """MUST: 두 사용자가 등록한 기간이 모두 보인다 (REQ-010, D-09, LB-021)."""
        base_time = now_kst().replace(second=0, microsecond=0)

        # 사용자 1이 등록한 쉬는 기간
        payload_user1 = {
            "start_at": format_kst(base_time + timedelta(days=1)),
            "end_at": format_kst(base_time + timedelta(days=3)),
        }
        code1, body1 = simulate_http_request(
            self.server, "POST", "/api/off-periods", secret=self.shared_secret, payload=payload_user1
        )
        self.assertEqual(code1, 201)
        user1_id = body1["id"]

        # 사용자 2가 등록한 쉬는 기간 (동일 공유 비밀키, REQ-010, D-09)
        payload_user2 = {
            "start_at": format_kst(base_time + timedelta(days=5)),
            "end_at": format_kst(base_time + timedelta(days=7)),
        }
        code2, body2 = simulate_http_request(
            self.server, "POST", "/api/off-periods", secret=self.shared_secret, payload=payload_user2
        )
        self.assertEqual(code2, 201)
        user2_id = body2["id"]

        # 두 사용자 중 누구든 쉬는 기간 목록을 조회하면 두 기간이 모두 보여야 한다
        code_get, body_get = simulate_http_request(
            self.server, "GET", "/api/off-periods", secret=self.shared_secret
        )
        self.assertEqual(code_get, 200)
        items = body_get.get("off_periods", [])
        self.assertEqual(len(items), 2, "두 사용자가 등록한 기간 2건이 모두 조회되어야 함")

        returned_ids = [item["id"] for item in items]
        self.assertIn(user1_id, returned_ids, "사용자 1이 등록한 쉬는 기간이 목록에 포함되어야 함")
        self.assertIn(user2_id, returned_ids, "사용자 2가 등록한 쉬는 기간이 목록에 포함되어야 함")

    def test_done_when_start_and_end_datetimes_visible(self) -> None:
        """done_when: 등록된 기간들이 시작·종료 일시와 함께 보인다 (REQ-012)."""
        item_tree = ET.parse(RES_LAYOUT / "item_off_period.xml")
        ns = "http://schemas.android.com/apk/res/android"
        ids = {e.attrib.get(f"{{{ns}}}id", "").split("/")[-1] for e in item_tree.iter()}
        self.assertIn("textStartAt", ids, "시작 일시 표시용 textStartAt 누락")
        self.assertIn("textEndAt", ids, "종료 일시 표시용 textEndAt 누락")

        kt_source = (SRC_JAVA / "OffPeriodListActivity.kt").read_text(encoding="utf-8")
        self.assertIn("textStartAt.text", kt_source)
        self.assertIn("textEndAt.text", kt_source)
        self.assertIn("formatStartKst()", kt_source)
        self.assertIn("formatEndKst()", kt_source)

    def test_off_period_item_model_and_kst_formatting(self) -> None:
        """OffPeriodItem 모델의 파싱, KST 포맷팅 및 상태 판정(진행 중/예정/종료) 검증."""
        model_source = (SRC_JAVA / "model/OffPeriodItem.kt").read_text(encoding="utf-8")
        for method in ("parseStartAt", "parseEndAt", "formatStartKst", "formatEndKst", "formatRangeKst", "getStatus"):
            self.assertIn(f"fun {method}", model_source, f"{method} 메서드 누락")

        # 세 가지 뱃지 상태 상수 검증
        self.assertIn('"진행 중"', model_source)
        self.assertIn('"예정"', model_source)
        self.assertIn('"종료"', model_source)

    def test_badge_colors_declared_in_colors_xml(self) -> None:
        """colors.xml에 쉬는 기간 상태 뱃지 전용 색상 정의 검증."""
        colors_tree = ET.parse(RES_VALUES / "colors.xml")
        color_names = {elem.attrib["name"] for elem in colors_tree.findall("color")}
        required_colors = [
            "status_off_active", "status_off_active_bg", "badge_border_off_active",
            "status_off_upcoming", "status_off_upcoming_bg", "badge_border_off_upcoming",
            "status_off_ended", "status_off_ended_bg", "badge_border_off_ended",
        ]
        for rc in required_colors:
            self.assertIn(rc, color_names, f"colors.xml에 '{rc}' 누락")

    def test_server_client_fetch_off_periods(self) -> None:
        """ServerClient.fetchOffPeriods 메서드와 헤더 및 엔드포인트 계약 검증."""
        client_code = (SRC_JAVA / "network/ServerClient.kt").read_text(encoding="utf-8")
        self.assertIn("fun fetchOffPeriods(", client_code)
        self.assertIn("/api/off-periods", client_code)
        self.assertIn("X-Shared-Secret", client_code)
        self.assertIn("Authorization", client_code)

    def test_unauthorized_get_rejected_with_401(self) -> None:
        """REQ-024: 올바른 비밀키 없이 들어온 조회 요청은 401 거부."""
        code, _ = simulate_http_request(self.server, "GET", "/api/off-periods", secret="wrong-secret")
        self.assertEqual(code, 401)

    def test_layout_elements_and_fold8_responsive_constraint(self) -> None:
        """activity_off_period_list.xml 필수 뷰 ID 및 640dp 반응형 제약 검증 (REQ-028, REQ-029)."""
        act_tree = ET.parse(RES_LAYOUT / "activity_off_period_list.xml")
        ns = "http://schemas.android.com/apk/res/android"
        act_ids = {elem.attrib.get(f"{{{ns}}}id", "").split("/")[-1] for elem in act_tree.iter()}

        required_ids = {
            "btnRefresh", "btnAddOffPeriod", "progressBar", "cardError", "textErrorMessage",
            "textListHeader", "textEmptyList", "layoutOffPeriodList",
        }
        self.assertTrue(required_ids.issubset(act_ids), f"누락된 뷰 ID: {required_ids - act_ids}")

        app_ns = "{http://schemas.android.com/apk/res-auto}"
        container = act_tree.find(f".//LinearLayout[@{app_ns}layout_constraintWidth_max='640dp']")
        self.assertIsNotNone(container, "Fold8/S21+ 반응형 640dp 최대 너비 제약 누락")

    def test_main_activity_and_manifest_registration(self) -> None:
        """MainActivity의 쉬는 기간 목록 버튼 및 AndroidManifest 등록 검증."""
        main_tree = ET.parse(RES_LAYOUT / "activity_main.xml")
        ns = "http://schemas.android.com/apk/res/android"
        main_ids = {elem.attrib.get(f"{{{ns}}}id", "").split("/")[-1] for elem in main_tree.iter()}
        self.assertIn("btnOffPeriodList", main_ids, "activity_main.xml에 btnOffPeriodList 누락")

        main_kt = (SRC_JAVA / "MainActivity.kt").read_text(encoding="utf-8")
        self.assertIn("btnOffPeriodList", main_kt)
        self.assertIn("OffPeriodListActivity::class.java", main_kt)

        manifest_tree = ET.parse(MANIFEST_PATH)
        activity_names = [elem.attrib.get(f"{{{ns}}}name") for elem in manifest_tree.findall(".//activity")]
        self.assertIn(".OffPeriodListActivity", activity_names, "AndroidManifest.xml에 OffPeriodListActivity 누락")

        target = next(
            a for a in manifest_tree.findall(".//activity")
            if a.attrib.get(f"{{{ns}}}name") == ".OffPeriodListActivity"
        )
        self.assertEqual(target.attrib.get(f"{{{ns}}}exported"), "false")
        config = target.attrib.get(f"{{{ns}}}configChanges", "")
        self.assertTrue(all(k in config for k in ("screenSize", "smallestScreenSize", "screenLayout")))


if __name__ == "__main__":
    unittest.main()
