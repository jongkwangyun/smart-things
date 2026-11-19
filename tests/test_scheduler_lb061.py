from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.models import STATUS_SCHEDULED, STATUS_SUCCESS, OffPeriod, Reservation
from server.off_periods.auto_stop import DEFAULT_DEVICE_ID
from server.scheduler import ReservationScheduler, get_scheduler_logger
from server.smartthings.auth import (
    SmartThingsTokenManager,
    TokenInfo,
    get_smartthings_logger,
    save_token_to_file,
)
from server.smartthings.client import DeviceStatus, SmartThingsApiError, SmartThingsAuthError, SmartThingsClient
from server.storage import Storage
from server.time_utils import KST, now_kst


def _close_logger_handler(logger_name: str, log_path: Path) -> None:
    logger = logging.getLogger(logger_name)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == log_path.resolve():
            h.flush()
            h.close()
            logger.removeHandler(h)


class TestSchedulerLB061(unittest.TestCase):
    """LB-061: 401 인증 백오프·즉시 복구·step당 상태 조회 1회 공유·로그 로테이션 검증."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = Path(self.temp_dir.name)
        self.db_path = self.tmp / "lb061.db"
        self.storage = Storage(self.db_path)
        self.sched_log = self.tmp / "scheduler_lb061.log"
        self.st_log = self.tmp / "smartthings_lb061.log"
        self.env_path = self.tmp / ".env.test"
        self.token_file = self.tmp / "st_token.json"
        self.base_time = datetime(2026, 11, 19, 10, 0, 0, tzinfo=KST)

    def tearDown(self) -> None:
        self.storage.close()
        _close_logger_handler("server.scheduler", self.sched_log)
        _close_logger_handler("server.smartthings", self.st_log)
        self.temp_dir.cleanup()

    def _http_401(self) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            url="https://api.smartthings.com/v1/devices/x/status",
            code=401,
            msg="Unauthorized",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error": "invalid_token"}'),
        )

    def _ok_status_resp(self, op_state: str = "idle") -> MagicMock:
        payload = {
            "components": {
                "main": {
                    "robotCleanerOperatingState": {"operatingState": {"value": op_state}},
                    "battery": {"battery": {"value": 95}},
                    "switch": {"switch": {"value": "off"}},
                    "samsungce.robotCleanerCleaningType": {"cleaningType": {"value": "mopAfterVacuum"}},
                    "custom.doNotDisturbMode": {
                        "doNotDisturb": {"value": "off"},
                        "startTime": {"value": "0000"},
                        "endTime": {"value": "0000"},
                    },
                }
            }
        }
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode("utf-8")
        resp.__enter__.return_value = resp
        return resp

    def test_consecutive_401_backoff_and_env_instant_recovery(self) -> None:
        """연속 401 시 매초 재시도가 백오프되고 .env 갱신 시 즉시 복구된다."""
        self.env_path.write_text("SMARTTHINGS_TOKEN=bad-pat-v1\n", encoding="utf-8")
        mgr = SmartThingsTokenManager.from_env(
            env_path=self.env_path, log_file=self.st_log, token_file=self.token_file,
        )
        client = SmartThingsClient(token_manager=mgr, simulated=False)
        scheduler = ReservationScheduler(
            storage=self.storage,
            client=client,
            device_id=DEFAULT_DEVICE_ID,
            now_func=lambda: self.base_time,
            log_file=self.sched_log,
            auth_backoff_base=10.0,
            auth_backoff_max=60.0,
        )

        with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: (_ for _ in ()).throw(self._http_401())) as m:
            # t=0s: 1회 실패 -> 10초 백오프 진입
            scheduler.step(self.base_time)
            self.assertEqual(m.call_count, 1)
            self.assertEqual(scheduler.auth_backoff.failure_count, 1)

            # t=1s..9s: 매초 호출되어도 API 호출 0회 추가
            for sec in range(1, 10):
                scheduler.step(self.base_time + timedelta(seconds=sec))
            self.assertEqual(m.call_count, 1)

            # t=10s: 백오프 만료 -> 1회 재시도 후 다시 실패(20초 백오프로 증가)
            scheduler.step(self.base_time + timedelta(seconds=10))
            self.assertEqual(m.call_count, 2)
            self.assertEqual(scheduler.auth_backoff.failure_count, 2)

            scheduler.step(self.base_time + timedelta(seconds=15))
            self.assertEqual(m.call_count, 2)

        # 백오프 중(t=16s) .env 갱신 시 즉시 백오프 해제 및 재시도 성공
        self.env_path.write_text("SMARTTHINGS_TOKEN=good-pat-v2\n", encoding="utf-8")
        with patch("urllib.request.urlopen", return_value=self._ok_status_resp("idle")) as m_ok:
            scheduler.step(self.base_time + timedelta(seconds=16))
            self.assertEqual(m_ok.call_count, 1)
            self.assertEqual(scheduler.auth_backoff.failure_count, 0)
            self.assertFalse(scheduler.auth_backoff.is_backing_off)

    def test_backoff_instant_recovery_on_token_file_update(self) -> None:
        """백오프 중 st_token.json 생성/갱신 시 즉시 백오프를 해제하고 새 OAuth 토큰으로 복구한다."""
        self.env_path.write_text("SMARTTHINGS_TOKEN=bad-pat-only\n", encoding="utf-8")
        mgr = SmartThingsTokenManager.from_env(
            env_path=self.env_path, log_file=self.st_log, token_file=self.token_file,
        )
        client = SmartThingsClient(token_manager=mgr, simulated=False)
        scheduler = ReservationScheduler(
            storage=self.storage,
            client=client,
            device_id=DEFAULT_DEVICE_ID,
            log_file=self.sched_log,
            auth_backoff_base=30.0,
        )

        with patch("urllib.request.urlopen", side_effect=self._http_401()):
            scheduler.step(self.base_time)
            self.assertTrue(scheduler.auth_backoff.is_backing_off)

        # st_token.json 생성으로 즉시 복구 (백오프 30초 만료 전 t=2s)
        save_token_to_file(
            TokenInfo(
                access_token="oauth-access-fresh",
                token_type="oauth",
                refresh_token="oauth-refresh-fresh",
                expires_at=now_kst() + timedelta(hours=12),
            ),
            self.token_file,
        )
        with patch("urllib.request.urlopen", return_value=self._ok_status_resp("idle")) as m_ok:
            scheduler.step(self.base_time + timedelta(seconds=2))
            self.assertEqual(m_ok.call_count, 1)
            self.assertEqual(mgr.token_info.access_token, "oauth-access-fresh")
            self.assertEqual(scheduler.auth_backoff.failure_count, 0)

    def test_single_get_device_status_per_step_shared_across_all_consumers(self) -> None:
        """1회 step() 당 get_device_status()를 최대 1회만 호출해 모든 소비자가 공유한다."""
        mgr = SmartThingsTokenManager(
            token_info=TokenInfo(access_token="sim-tok", token_type="pat"),
            log_file=self.st_log,
            env_path=self.env_path,
        )
        client = SmartThingsClient(token_manager=mgr, simulated=True)
        client.simulated_dnd = {"startTime": "2200", "endTime": "0700", "doNotDisturb": "off"}

        # 활성 쉬는 기간 + 허용된 실행 중 예약 등록 (DndSync, AutoStop, WaterSessionTracker, 예약 성공 판정 모두 활성)
        self.storage.save_off_period(OffPeriod(
            id="op-share",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        ))
        self.storage.save_reservation(Reservation(
            id="res-share",
            reserved_at=self.base_time - timedelta(seconds=30),
            status=STATUS_SCHEDULED,
            allow_during_off_period=True,
            executed_at=self.base_time - timedelta(seconds=30),
        ))

        status_calls: list[str] = []
        orig_get_status = client.get_device_status

        def counted_get_status(dev_id: str) -> DeviceStatus:
            status_calls.append(dev_id)
            st = orig_get_status(dev_id)
            st.operating_state = "cleaning"
            return st

        scheduler = ReservationScheduler(
            storage=self.storage,
            client=client,
            device_id=DEFAULT_DEVICE_ID,
            now_func=lambda: self.base_time,
            log_file=self.sched_log,
        )

        with patch.object(client, "get_device_status", side_effect=counted_get_status):
            scheduler.step(self.base_time)
        self.assertEqual(len(status_calls), 1)
        res_after = self.storage.get_reservation("res-share")
        assert res_after is not None
        self.assertEqual(res_after.status, STATUS_SUCCESS)
        backup = self.storage.get_dnd_backup()
        self.assertIsNotNone(backup)
        assert backup is not None
        self.assertEqual(backup["startTime"], "2200")
        self.assertEqual(self.storage.get_water_tracker().mop_clean_count, 1)

        # 다음 step 에서 기기 상태 조회 실패 시에도 1회만 시도하고 중복 조회하지 않음
        def failing_status(dev_id: str) -> DeviceStatus:
            status_calls.append(dev_id)
            raise SmartThingsApiError("네트워크 일시 오류")

        with patch.object(client, "get_device_status", side_effect=failing_status):
            scheduler.step(self.base_time + timedelta(seconds=5))
        self.assertEqual(len(status_calls), 2)

    def test_rotating_file_handler_applied_and_rotates(self) -> None:
        """scheduler.log 와 smartthings.log 모두 RotatingFileHandler가 적용되고 실제 로테이션된다."""
        sched_logger = get_scheduler_logger(self.sched_log, max_bytes=220, backup_count=2)
        st_logger = get_smartthings_logger(self.st_log, max_bytes=220, backup_count=2)

        sched_handlers = [
            h for h in sched_logger.handlers
            if isinstance(h, RotatingFileHandler) and Path(h.baseFilename).resolve() == self.sched_log.resolve()
        ]
        st_handlers = [
            h for h in st_logger.handlers
            if isinstance(h, RotatingFileHandler) and Path(h.baseFilename).resolve() == self.st_log.resolve()
        ]
        self.assertEqual(len(sched_handlers), 1)
        self.assertEqual(len(st_handlers), 1)

        for i in range(15):
            sched_logger.info("스케줄러 로테이션 테스트 메시지 #%02d - %s", i, "x" * 30)
            st_logger.info("스마트싱스 로테이션 테스트 메시지 #%02d - %s", i, "y" * 30)

        for h in (*sched_handlers, *st_handlers):
            h.flush()

        self.assertTrue((self.tmp / "scheduler_lb061.log.1").is_file())
        self.assertTrue((self.tmp / "smartthings_lb061.log.1").is_file())
        self.assertLessEqual(self.sched_log.stat().st_size, 220)
        self.assertLessEqual(self.st_log.stat().st_size, 220)
        self.assertFalse((self.tmp / "scheduler_lb061.log.3").exists())

    def test_active_off_period_dnd_status_failure_no_duplicate_calls(self) -> None:
        """활성 쉬는 기간(backup 없음) 중 401 또는 API 오류 시 get_device_status 1회만 호출된다."""
        self.storage.save_off_period(OffPeriod(
            id="op-dnd-fail",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        ))
        mgr = SmartThingsTokenManager(
            token_info=TokenInfo(access_token="tok", token_type="pat"),
            log_file=self.st_log,
            env_path=self.env_path,
        )
        client = SmartThingsClient(token_manager=mgr, simulated=True)
        scheduler = ReservationScheduler(
            storage=self.storage,
            client=client,
            device_id=DEFAULT_DEVICE_ID,
            now_func=lambda: self.base_time,
            log_file=self.sched_log,
        )

        # 1) 401 발생: step() 내 get_device_status 1회만 호출 및 failure_count == 1
        with patch.object(client, "get_device_status", side_effect=SmartThingsAuthError("401")) as m_auth:
            scheduler.step(self.base_time)
            self.assertEqual(m_auth.call_count, 1)
            self.assertEqual(scheduler.auth_backoff.failure_count, 1)
            self.assertIsNone(self.storage.get_dnd_backup())

        # 2) 백오프 리셋 후 일반 API 오류 발생: DndSyncManager로 인해 재호출되지 않고 1회만 호출
        scheduler.auth_backoff.reset()
        with patch.object(client, "get_device_status", side_effect=SmartThingsApiError("timeout")) as m_api:
            scheduler.step(self.base_time + timedelta(seconds=1))
            self.assertEqual(m_api.call_count, 1)
            self.assertEqual(scheduler.auth_backoff.failure_count, 0)
            self.assertIsNone(self.storage.get_dnd_backup())

    def test_overdue_reservation_mark_failure_401_keeps_step_halted(self) -> None:
        """_mark_failure 내 get_device_status 401 시 같은 step의 후속 API 호출이 중단된다."""
        self.storage.save_off_period(OffPeriod(
            id="op-overdue",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        ))
        self.storage.save_reservation(Reservation(
            id="res-overdue-401",
            reserved_at=self.base_time - timedelta(minutes=10),
            status=STATUS_SCHEDULED,
            executed_at=self.base_time - timedelta(minutes=10),
        ))
        mgr = SmartThingsTokenManager(
            token_info=TokenInfo(access_token="tok", token_type="pat"),
            log_file=self.st_log,
            env_path=self.env_path,
        )
        client = SmartThingsClient(token_manager=mgr, simulated=True)
        scheduler = ReservationScheduler(
            storage=self.storage,
            client=client,
            device_id=DEFAULT_DEVICE_ID,
            now_func=lambda: self.base_time,
            log_file=self.sched_log,
        )

        with patch.object(client, "get_device_health", return_value="ONLINE"):
            with patch.object(client, "get_device_status", side_effect=SmartThingsAuthError("401")) as m_status:
                scheduler.step(self.base_time)
                self.assertEqual(m_status.call_count, 1)
                self.assertEqual(scheduler.auth_backoff.failure_count, 1)
                self.assertTrue(scheduler.auth_backoff.is_backing_off)
                self.assertIsNone(self.storage.get_dnd_backup())

    def test_stale_watcher_last_auth_error_does_not_cause_infinite_backoff(self) -> None:
        """AutoStopWatcher/DndSyncManager의 이전 last_auth_error가 후속 정상 step에서 백오프를 재유발하지 않는다."""
        self.storage.save_off_period(OffPeriod(
            id="op-stale",
            start_at=self.base_time - timedelta(minutes=10),
            end_at=self.base_time + timedelta(minutes=1),
        ))
        self.storage.save_dnd_backup({"startTime": "0000", "endTime": "0000", "doNotDisturb": "off"})
        mgr = SmartThingsTokenManager(
            token_info=TokenInfo(access_token="tok", token_type="pat"),
            log_file=self.st_log,
            env_path=self.env_path,
        )
        client = SmartThingsClient(token_manager=mgr, simulated=True)
        scheduler = ReservationScheduler(
            storage=self.storage,
            client=client,
            device_id=DEFAULT_DEVICE_ID,
            now_func=lambda: self.base_time,
            log_file=self.sched_log,
            auth_backoff_base=5.0,
        )

        # 1) AutoStopWatcher.return_to_home()가 401 발생 -> failure_count == 1 및 백오프 진입
        cleaning_st = DeviceStatus(device_id=DEFAULT_DEVICE_ID, operating_state="cleaning", is_online=True)
        with patch.object(client, "get_device_status", return_value=cleaning_st):
            with patch.object(client, "return_to_home", side_effect=SmartThingsAuthError("401")):
                scheduler.step(self.base_time)
        self.assertEqual(scheduler.auth_backoff.failure_count, 1)
        self.assertIsNone(scheduler.auto_stop_watcher.last_auth_error)

        # 2) 외부 호출 등으로 stale last_auth_error가 남았더라도, get_device_status가 일반 API 오류일 때 백오프 미발생
        scheduler.auth_backoff.reset()
        scheduler.auto_stop_watcher.last_auth_error = SmartThingsAuthError("stale-autostop-401")
        scheduler.dnd_sync_manager.last_auth_error = SmartThingsAuthError("stale-dnd-401")
        with patch.object(client, "get_device_status", side_effect=SmartThingsApiError("timeout")):
            scheduler.step(self.base_time + timedelta(seconds=1))
        self.assertEqual(scheduler.auth_backoff.failure_count, 0)
        self.assertFalse(scheduler.auth_backoff.is_backing_off)
        self.assertIsNone(scheduler.auto_stop_watcher.last_auth_error)
        self.assertIsNone(scheduler.dnd_sync_manager.last_auth_error)

        # 3) OffPeriod 종료 + 백오프 만료 후 정상 step() -> 백오프 해제되고 WaterSessionTracker 실행 확인
        scheduler.auto_stop_watcher.last_auth_error = SmartThingsAuthError("stale-autostop-401")
        with patch.object(client, "get_device_status", return_value=cleaning_st):
            scheduler.step(self.base_time + timedelta(minutes=2))
        self.assertEqual(scheduler.auth_backoff.failure_count, 0)
        self.assertFalse(scheduler.auth_backoff.is_backing_off)
        self.assertEqual(self.storage.get_water_tracker().mop_clean_count, 1)


if __name__ == "__main__":
    unittest.main()
