from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.config import Config
from server.models import CLEANING_TYPE_VACUUM, DEFAULT_CLEANING_TYPE, STATUS_SCHEDULED, OffPeriod, Reservation
from server.notify import FCMClient, NotificationService
from server.off_periods.auto_stop import AutoStopWatcher
from server.scheduler import ReservationScheduler
from server.server import MiniPCHandler, MiniPCServer
from server.smartthings.client import DeviceStatus
from server.storage import Storage
from server.time_utils import KST
from server.water.service import WaterService
from server.water.session_tracker import WaterSessionTracker

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class MockClient:
    """물청소 세션 테스트용 스마트싱스 클라이언트."""

    def __init__(self, operating_state: str = "idle", cleaning_type: str = "mopAfterVacuum") -> None:
        self.operating_state = operating_state
        self.cleaning_type = cleaning_type
        self.stopped = False

    def get_device_status(self, device_id: str) -> DeviceStatus:
        return DeviceStatus(
            device_id=device_id,
            operating_state=self.operating_state,
            cleaning_type=self.cleaning_type,
            raw_status={
                "components": {
                    "main": {
                        "robotCleanerOperatingState": {"operatingState": {"value": self.operating_state}},
                        "samsungce.robotCleanerCleaningType": {"cleaningType": {"value": self.cleaning_type}},
                    }
                }
            },
        )

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        self.operating_state = "cleaning"
        return {"results": [{"id": f"cmd-start-{device_id}", "status": "COMPLETED"}]}

    def return_to_home(self, device_id: str) -> dict[str, Any]:
        self.stopped = True
        self.operating_state = "homing"
        return {"results": [{"id": f"cmd-stop-{device_id}", "status": "COMPLETED"}]}


class MockSocket:
    def __init__(self, data: bytes) -> None:
        self.rfile = io.BytesIO(data)
        self.wfile = io.BytesIO()
        self._sock = self

    def makefile(self, mode: str, *args: Any, **kwargs: Any) -> io.BytesIO:
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data: bytes) -> None:
        self.wfile.write(data)


def simulate_request(
    server: MiniPCServer, method: str, path: str, secret: str = "test-secret", body: dict[str, Any] | None = None
) -> tuple[int, dict[str, Any]]:
    headers = ["Host: 127.0.0.1", f"X-Shared-Secret: {secret}"]
    payload_bytes = b""
    if body is not None:
        payload_bytes = json.dumps(body).encode("utf-8")
        headers.extend([f"Content-Length: {len(payload_bytes)}", "Content-Type: application/json"])
    raw = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n" + payload_bytes.decode("utf-8")
    sock = MockSocket(raw.encode("utf-8"))
    MiniPCHandler(sock, ("127.0.0.1", 12345), server)  # type: ignore[arg-type]
    parts = sock.wfile.getvalue().split(b"\r\n\r\n", 1)
    status = int(parts[0].split(b"\r\n")[0].decode("utf-8").split(" ")[1])
    data = json.loads(parts[1].decode("utf-8")) if len(parts) > 1 and parts[1] else {}
    return status, data


class TestWaterCalibration(unittest.TestCase):
    """LB-059: 물청소 세션 자동 감지 및 물 채움 시 기준 N 자동 보정 테스트 (REQ-019~021)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test_calibration.db"
        self.storage = Storage(self.db_path)
        self.secret = "test-secret"
        self.config = Config(host="127.0.0.1", port=0, shared_secret=self.secret, db_path=self.db_path)
        self.fcm = FCMClient(simulated=True)
        self.notify = NotificationService(self.storage, self.fcm)
        self.server = MiniPCServer(self.config, self.storage, notification_service=self.notify)
        self.water_service = WaterService(self.storage, notification_service=self.notify)

    def tearDown(self) -> None:
        self.server.server_close()
        self.storage.close()
        self.temp_dir.cleanup()

    def test_refill_calibration_formula(self) -> None:
        """물 채움 시 K>=1이면 N=max(1, K-1), K=0이면 N 유지 검증 (REQ-019, REQ-021)."""
        tracker = self.storage.get_water_tracker()
        tracker.alert_threshold_n = 7

        # K=0: calibrate_threshold=True 여도 N=7 유지 (Must rule)
        tracker.mop_clean_count = 0
        self.storage.save_water_tracker(tracker)
        self.assertEqual(self.water_service.refill_water(calibrate_threshold=True).alert_threshold_n, 7)

        # K=1: max(1, 1-1) = 1
        tracker.mop_clean_count = 1
        self.storage.save_water_tracker(tracker)
        self.assertEqual(self.water_service.refill_water(calibrate_threshold=True).alert_threshold_n, 1)

        # K=2: max(1, 2-1) = 1
        tracker.alert_threshold_n, tracker.mop_clean_count = 10, 2
        self.storage.save_water_tracker(tracker)
        self.assertEqual(self.water_service.refill_water(calibrate_threshold=True).alert_threshold_n, 1)

        # K=7: max(1, 7-1) = 6
        tracker.alert_threshold_n, tracker.mop_clean_count = 10, 7
        self.storage.save_water_tracker(tracker)
        res7 = self.water_service.refill_water(calibrate_threshold=True)
        self.assertEqual(res7.alert_threshold_n, 6)
        self.assertEqual(res7.mop_clean_count, 0)

        # calibrate_threshold=False 일 때는 K=10이어도 기존 N 유지
        tracker.alert_threshold_n, tracker.mop_clean_count = 6, 10
        self.storage.save_water_tracker(tracker)
        self.assertEqual(self.water_service.refill_water(calibrate_threshold=False).alert_threshold_n, 6)

    def test_http_refill_api_json_and_query_param(self) -> None:
        """POST /api/water-tracker/refill 요청 시 JSON 및 쿼리 파라미터 수용 검증."""
        tracker = self.storage.get_water_tracker()
        tracker.mop_clean_count, tracker.alert_threshold_n = 5, 10
        self.storage.save_water_tracker(tracker)

        # 1. JSON payload: {"calibrate_threshold": true} -> N: 4, count: 0
        s1, b1 = simulate_request(self.server, "POST", "/api/water-tracker/refill", body={"calibrate_threshold": True})
        self.assertEqual((s1, b1["alert_threshold_n"], b1["mop_clean_count"]), (200, 4, 0))

        # 2. Query param: ?calibrate_threshold=true -> K=3 이면 N: 2
        tracker = self.storage.get_water_tracker()
        tracker.mop_clean_count = 3
        self.storage.save_water_tracker(tracker)
        s2, b2 = simulate_request(self.server, "POST", "/api/water-tracker/refill?calibrate_threshold=true")
        self.assertEqual((s2, b2["alert_threshold_n"], b2["mop_clean_count"]), (200, 2, 0))

        # 3. 생략 시 기본값 False -> N(2) 유지
        tracker = self.storage.get_water_tracker()
        tracker.mop_clean_count = 8
        self.storage.save_water_tracker(tracker)
        s3, b3 = simulate_request(self.server, "POST", "/api/water-tracker/refill")
        self.assertEqual((s3, b3["alert_threshold_n"], b3["mop_clean_count"]), (200, 2, 0))

    def test_external_mop_session_auto_detection_and_washing_dedup(self) -> None:
        """외부 시작 물청소 세션 자동 감지 및 washingMop 등 세션 내 중복 방지 (REQ-020, LB-059)."""
        client = MockClient(operating_state="idle", cleaning_type="vacuumAndMopTogether")
        tracker = WaterSessionTracker(self.storage, self.water_service, client=client)  # type: ignore[arg-type]
        now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=KST)

        tracker.step(now)
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 0)
        self.assertFalse(tracker.is_in_session)

        # 외부 청소 시작: cleaning -> 1회 카운트
        client.operating_state = "cleaning"
        tracker.step(now + timedelta(seconds=1))
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 1)
        self.assertTrue(tracker.is_in_session)

        # 동일 세션 유지 (cleaning -> washingMop -> cleaning): 중복 증가 없음
        client.operating_state = "washingMop"
        tracker.step(now + timedelta(seconds=10))
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 1)

        client.operating_state = "cleaning"
        tracker.step(now + timedelta(seconds=15))
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 1)

        # 청소 완료 복귀 (charging): 세션 종료
        client.operating_state = "charging"
        tracker.step(now + timedelta(seconds=20))
        self.assertFalse(tracker.is_in_session)
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 1)

        # 새 세션 시작: 카운트 2로 증가
        client.operating_state = "cleaning"
        tracker.step(now + timedelta(seconds=25))
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 2)

    def test_vacuum_only_session_excluded(self) -> None:
        """먼지 단독 청소(vacuum) 세션은 물걸레 카운트에서 제외 (Must rule, REQ-020)."""
        client = MockClient(operating_state="cleaning", cleaning_type=CLEANING_TYPE_VACUUM)
        tracker = WaterSessionTracker(self.storage, self.water_service, client=client)  # type: ignore[arg-type]
        now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=KST)

        tracker.step(now)
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 0)
        self.assertTrue(tracker.is_in_session)

        client.operating_state = "idle"
        tracker.step(now + timedelta(seconds=5))
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 0)

    def test_off_period_autostop_session_excluded(self) -> None:
        """쉬는 기간 즉시 정지(autostop) 세션은 물청소 카운트 제외 (Must rule)."""
        client = MockClient(operating_state="cleaning", cleaning_type=DEFAULT_CLEANING_TYPE)
        watcher = AutoStopWatcher(self.storage, client)  # type: ignore[arg-type]
        tracker = WaterSessionTracker(self.storage, self.water_service, client=client, auto_stop_watcher=watcher)  # type: ignore[arg-type]
        now = datetime(2026, 11, 12, 14, 0, 0, tzinfo=KST)

        self.storage.save_off_period(
            OffPeriod(
                id="op-test",
                start_at=datetime(2026, 11, 12, 13, 0, 0, tzinfo=KST),
                end_at=datetime(2026, 11, 12, 15, 0, 0, tzinfo=KST),
            )
        )
        tracker.step(now)
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 0)

    def test_reservation_and_external_deduplication(self) -> None:
        """이 앱 1회성 예약 실행 성공과 외부 감지 간 중복 방지 일원화 검증 (Must rule)."""
        client = MockClient(operating_state="cleaning", cleaning_type=DEFAULT_CLEANING_TYPE)
        scheduler = ReservationScheduler(
            storage=self.storage, client=client, notification_service=self.notify, water_service=self.water_service  # type: ignore[arg-type]
        )
        now = datetime(2026, 11, 12, 10, 0, 0, tzinfo=KST)
        scheduler.now_func = lambda: now

        self.storage.save_reservation(
            Reservation(id="res-dedup-1", reserved_at=now - timedelta(seconds=10), status=STATUS_SCHEDULED)
        )
        scheduler.step()
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 1)

        # 다음 스케줄러 루프 실행 시 기기가 여전히 cleaning 상태여도 중복 카운트 안 됨
        scheduler.step()
        self.assertEqual(self.water_service.get_tracker().mop_clean_count, 1)

    def test_file_line_counts(self) -> None:
        """모든 관련 파일이 300줄 이하를 엄격 준수하는지 확인."""
        checked = [
            PROJECT_ROOT / "server/water/service.py",
            PROJECT_ROOT / "server/water/session_tracker.py",
            PROJECT_ROOT / "server/scheduler/scheduler.py",
            PROJECT_ROOT / "server/server.py",
            PROJECT_ROOT / "server/smartthings/client.py",
            Path(__file__),
        ]
        for p in checked:
            lines = len(p.read_text(encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 300, f"{p.name} 줄 수 초과 ({lines} > 300)")


if __name__ == "__main__":
    unittest.main()
