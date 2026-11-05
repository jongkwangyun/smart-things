from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from server.models import OffPeriod
from server.off_periods.auto_stop import DEFAULT_DEVICE_ID
from server.off_periods.dnd_sync import DndSyncManager
from server.off_periods.service import OffPeriodService
from server.scheduler.scheduler import ReservationScheduler
from server.smartthings.auth import SmartThingsTokenManager, TokenInfo
from server.smartthings.client import SmartThingsApiError, SmartThingsClient
from server.storage import Storage
from server.time_utils import format_kst, now_kst


def make_dummy_client(initial_dnd: dict[str, str] | None = None) -> SmartThingsClient:
    t_info = TokenInfo(access_token="test-dnd-token", token_type="pat")
    tm = SmartThingsTokenManager(token_info=t_info)
    client = SmartThingsClient(token_manager=tm, simulated=True)
    if initial_dnd is not None:
        client.simulated_dnd = dict(initial_dnd)
    return client


class TestDndSyncManager(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.tmp.name) / "test_dnd.db"
        self.storage = Storage(self.db_path)
        self.client = make_dummy_client({"startTime": "1200", "endTime": "2100", "doNotDisturb": "off"})
        self.dnd_manager = DndSyncManager(
            storage=self.storage,
            client=self.client,
            device_id=DEFAULT_DEVICE_ID,
        )
        self.base_time = now_kst().replace(second=0, microsecond=0)

    def tearDown(self) -> None:
        self.storage.close()
        self.tmp.cleanup()

    def test_enter_off_period_enables_dnd_and_saves_backup(self) -> None:
        """진입 시 기존 DND 설정 백업 및 종일 DND(0000-2359, on) 활성화 (REQ-016)."""
        op = OffPeriod(
            id="off-1",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=2),
        )
        self.storage.save_off_period(op)

        # 1차 동기화: 백업 생성 및 DND 종일 on 설정
        result = self.dnd_manager.sync(self.base_time)
        self.assertTrue(result)

        # 백업 상태 검증
        backup = self.storage.get_dnd_backup()
        self.assertIsNotNone(backup)
        assert backup is not None
        self.assertEqual(backup["startTime"], "1200")
        self.assertEqual(backup["endTime"], "2100")
        self.assertEqual(backup["doNotDisturb"], "off")

        # 기기 DND 상태 검증
        dnd_state = self.client.get_dnd_status(DEFAULT_DEVICE_ID)
        self.assertEqual(dnd_state["startTime"], "0000")
        self.assertEqual(dnd_state["endTime"], "2359")
        self.assertEqual(dnd_state["doNotDisturb"], "on")

        # 2차 동기화: 이미 활성화 및 백업 완료 상태이므로 False 반환 (중복 방지)
        result_again = self.dnd_manager.sync(self.base_time)
        self.assertFalse(result_again)

    def test_already_full_day_dnd_avoids_redundant_commands(self) -> None:
        """이미 종일 DND on 상태인 경우 중복 명령 없이 백업만 저장한다."""
        self.client.simulated_dnd = {"startTime": "0000", "endTime": "2359", "doNotDisturb": "on"}
        op = OffPeriod(
            id="off-full",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        )
        self.storage.save_off_period(op)

        self.client.set_dnd_mode = MagicMock(wraps=self.client.set_dnd_mode)  # type: ignore[method-assign]
        res = self.dnd_manager.sync(self.base_time)
        self.assertTrue(res)
        self.client.set_dnd_mode.assert_not_called()
        self.assertIsNotNone(self.storage.get_dnd_backup())

    def test_exit_off_period_restores_dnd_and_clears_backup(self) -> None:
        """쉬는 기간 종료 시 백업된 원래 DND로 복원 및 백업 삭제 (REQ-016)."""
        op = OffPeriod(
            id="off-end",
            start_at=self.base_time - timedelta(hours=2),
            end_at=self.base_time - timedelta(hours=1),
        )
        self.storage.save_off_period(op)
        self.storage.save_dnd_backup({"startTime": "1300", "endTime": "1800", "doNotDisturb": "off"})
        self.client.simulated_dnd = {"startTime": "0000", "endTime": "2359", "doNotDisturb": "on"}

        res = self.dnd_manager.sync(self.base_time)
        self.assertTrue(res)

        # 복원 검증
        curr = self.client.get_dnd_status(DEFAULT_DEVICE_ID)
        self.assertEqual(curr["startTime"], "1300")
        self.assertEqual(curr["endTime"], "1800")
        self.assertEqual(curr["doNotDisturb"], "off")

        # 백업 삭제 검증
        self.assertIsNone(self.storage.get_dnd_backup())

        # 추가 동기화 시 동작 없음
        self.assertFalse(self.dnd_manager.sync(self.base_time))

    def test_delete_off_period_via_service_restores_dnd(self) -> None:
        """쉬는 기간 삭제 시 즉시 DND가 원래 상태로 복원된다."""
        op_svc = OffPeriodService(
            storage=self.storage,
            now_func=lambda: self.base_time,
            dnd_sync_manager=self.dnd_manager,
        )
        created = op_svc.create_off_period({
            "id": "off-del",
            "start_at": format_kst(self.base_time - timedelta(minutes=10)),
            "end_at": format_kst(self.base_time + timedelta(hours=1)),
        })
        # 생성 시 즉시 진입 DND 활성화 확인
        self.assertEqual(self.client.get_dnd_status(DEFAULT_DEVICE_ID)["doNotDisturb"], "on")
        self.assertIsNotNone(self.storage.get_dnd_backup())

        # 삭제 호출 시 즉시 복원 확인
        op_svc.delete_off_period(created.id)
        self.assertIsNone(self.storage.get_dnd_backup())
        curr = self.client.get_dnd_status(DEFAULT_DEVICE_ID)
        self.assertEqual(curr["startTime"], "1200")
        self.assertEqual(curr["endTime"], "2100")
        self.assertEqual(curr["doNotDisturb"], "off")

    def test_error_handling_when_client_fails(self) -> None:
        """기기 API 호출 오류 발생 시 안전하게 로깅하고 예외를 전파하지 않는다."""
        op = OffPeriod(
            id="off-err",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        )
        self.storage.save_off_period(op)

        def raise_err(*args: Any, **kwargs: Any) -> Any:
            raise SmartThingsApiError("기기 네트워크 장애 시뮬레이션")

        self.client.get_dnd_status = raise_err  # type: ignore[method-assign]
        res = self.dnd_manager.sync(self.base_time)
        self.assertFalse(res)

    def test_scheduler_step_invokes_dnd_sync_and_preserves_autostop(self) -> None:
        """ReservationScheduler.step()에서 DND 동기화가 호출되고 2차 AutoStopWatcher가 유지된다."""
        op = OffPeriod(
            id="off-sched",
            start_at=self.base_time - timedelta(hours=1),
            end_at=self.base_time + timedelta(hours=1),
        )
        self.storage.save_off_period(op)

        mock_watcher = MagicMock()
        scheduler = ReservationScheduler(
            storage=self.storage,
            client=self.client,
            now_func=lambda: self.base_time,
            auto_stop_watcher=mock_watcher,
            dnd_sync_manager=self.dnd_manager,
        )
        scheduler.step(self.base_time)

        # DND 활성화 확인
        self.assertIsNotNone(self.storage.get_dnd_backup())
        self.assertEqual(self.client.get_dnd_status(DEFAULT_DEVICE_ID)["doNotDisturb"], "on")

        # 2차 AutoStopWatcher 호출 유지 확인
        mock_watcher.step.assert_called_once_with(self.base_time)

    def test_sqlite_dnd_backup_persistence(self) -> None:
        """SQLite DB 재연결 후에도 DND 백업이 영구 보존된다."""
        backup_data = {"startTime": "0930", "endTime": "1830", "doNotDisturb": "off"}
        self.storage.save_dnd_backup(backup_data)
        self.storage.close()

        # 새로운 Storage 인스턴스로 동일 DB 파일 재접근
        new_storage = Storage(self.db_path)
        try:
            loaded = new_storage.get_dnd_backup()
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["startTime"], "0930")
            self.assertEqual(loaded["endTime"], "1830")
            self.assertEqual(loaded["doNotDisturb"], "off")
            self.assertEqual(loaded["mode"], "off")

            # 삭제 및 재접근 확인
            new_storage.clear_dnd_backup()
            self.assertIsNone(new_storage.get_dnd_backup())
        finally:
            new_storage.close()

    def test_client_dnd_command_payload_structure(self) -> None:
        """custom.doNotDisturbMode 커맨드 페이로드 및 인자 형식 검증."""
        captured_cmds: list[list[dict[str, Any]]] = []

        def mock_exec(dev_id: str, cmds: list[dict[str, Any]]) -> dict[str, Any]:
            captured_cmds.append(cmds)
            return {"results": [{"status": "COMPLETED"}]}

        self.client.execute_commands = mock_exec  # type: ignore[assignment]
        self.client.set_dnd_mode(DEFAULT_DEVICE_ID, "0000", "2359", "on")

        self.assertEqual(len(captured_cmds), 1)
        cmd = captured_cmds[0][0]
        self.assertEqual(cmd["component"], "main")
        self.assertEqual(cmd["capability"], "custom.doNotDisturbMode")
        self.assertEqual(cmd["command"], "setDoNotDisturbMode")
        self.assertEqual(cmd["arguments"], ["0000", "2359", "on"])


if __name__ == "__main__":
    unittest.main()
