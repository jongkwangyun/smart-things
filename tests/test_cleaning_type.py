from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from server.models import (
    CLEANING_TYPE_MOP,
    CLEANING_TYPE_MOP_AFTER_VACUUM,
    CLEANING_TYPE_VACUUM,
    CLEANING_TYPE_VACUUM_AND_MOP,
    DEFAULT_CLEANING_TYPE,
    STATUS_SCHEDULED,
    STATUS_SUCCESS,
    Reservation,
)
from server.reservations.service import ReservationService
from server.reservations.validator import (
    ReservationValidationError,
    validate_reservation_create_payload,
    validate_reservation_update_payload,
)
from server.scheduler.scheduler import ReservationScheduler
from server.smartthings.auth import SmartThingsTokenManager, TokenInfo
from server.smartthings.client import DeviceStatus, SmartThingsClient
from server.storage import Storage
from server.time_utils import KST, format_kst, now_kst


class TestCleaningTypeMigration(unittest.TestCase):
    """구 스키마 DB에서 cleaning_type 컬럼 추가 ALTER TABLE 마이그레이션 검증."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "migration_test.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_migration_adds_column_and_sets_default(self) -> None:
        # cleaning_type 컬럼이 없는 구 버전 스키마 직접 생성
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE reservations (
                id TEXT PRIMARY KEY, reserved_at TEXT NOT NULL, status TEXT NOT NULL,
                allow_during_off_period INTEGER NOT NULL DEFAULT 0, failure_reason TEXT,
                last_device_state TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, executed_at TEXT
            );
        """)
        now_str = format_kst(now_kst())
        conn.execute(
            """INSERT INTO reservations VALUES ('legacy-1', ?, '예정', 0, NULL, NULL, ?, ?, NULL);""",
            (now_str, now_str, now_str),
        )
        conn.commit()
        conn.close()

        # Storage 초기화 시 마이그레이션 실행 확인
        storage = Storage(self.db_path)
        try:
            res = storage.get_reservation("legacy-1")
            self.assertIsNotNone(res)
            assert res is not None
            self.assertEqual(res.cleaning_type, DEFAULT_CLEANING_TYPE)
            self.assertEqual(res.cleaning_type, "mopAfterVacuum")

            # 신규 예약 저장 및 조회 검증
            future = now_kst() + timedelta(hours=1)
            new_res = Reservation(
                id="new-1", reserved_at=future, cleaning_type=CLEANING_TYPE_VACUUM,
            )
            storage.save_reservation(new_res)
            fetched = storage.get_reservation("new-1")
            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertEqual(fetched.cleaning_type, CLEANING_TYPE_VACUUM)
        finally:
            storage.close()


class TestCleaningTypeValidationAndService(unittest.TestCase):
    """예약 생성/수정 API의 cleaning_type 유효성 검사 및 서비스 동작 검증."""

    def setUp(self) -> None:
        self.fixed_now = datetime(2026, 11, 12, 14, 0, 0, tzinfo=KST)
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(Path(self.temp_dir.name) / "test.db")
        self.service = ReservationService(self.storage, now_func=lambda: self.fixed_now)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_default_cleaning_type_on_create(self) -> None:
        # cleaning_type 미지정 시 mopAfterVacuum 기본값 적용
        payload = {"scheduled_at": "2026-11-12T14:30:00+09:00"}
        val = validate_reservation_create_payload(payload, now_func=lambda: self.fixed_now)
        self.assertEqual(val["cleaning_type"], DEFAULT_CLEANING_TYPE)

        res = self.service.create_reservation(payload)
        self.assertEqual(res.cleaning_type, DEFAULT_CLEANING_TYPE)
        self.assertEqual(res.to_dict()["cleaning_type"], DEFAULT_CLEANING_TYPE)

    def test_all_valid_cleaning_types_accepted(self) -> None:
        valid_types = [
            CLEANING_TYPE_VACUUM,
            CLEANING_TYPE_MOP,
            CLEANING_TYPE_VACUUM_AND_MOP,
            CLEANING_TYPE_MOP_AFTER_VACUUM,
        ]
        for idx, ctype in enumerate(valid_types):
            payload = {
                "id": f"res-type-{idx}",
                "scheduled_at": f"2026-11-12T15:{10 + idx:02d}:00+09:00",
                "cleaning_type": ctype,
            }
            res = self.service.create_reservation(payload)
            self.assertEqual(res.cleaning_type, ctype)
            fetched = self.service.get_reservation(res.id)
            self.assertIsNotNone(fetched)
            assert fetched is not None
            self.assertEqual(fetched.cleaning_type, ctype)

    def test_invalid_cleaning_type_rejected(self) -> None:
        for invalid in ["invalid", "dry", "dust", "", 123, None]:
            with self.assertRaises(ReservationValidationError):
                validate_reservation_create_payload(
                    {"scheduled_at": "2026-11-12T14:30:00+09:00", "cleaning_type": invalid},
                    now_func=lambda: self.fixed_now,
                )

    def test_forbidden_mode_and_zone_keys_still_rejected(self) -> None:
        # D-03: 구역 및 모드 선택 금지 유지
        for forbidden in ["mode", "cleaning_mode", "zone", "zones", "room", "area"]:
            with self.assertRaises(ReservationValidationError):
                validate_reservation_create_payload(
                    {
                        "scheduled_at": "2026-11-12T14:30:00+09:00",
                        "cleaning_type": CLEANING_TYPE_VACUUM,
                        forbidden: "kitchen",
                    },
                    now_func=lambda: self.fixed_now,
                )

    def test_update_cleaning_type(self) -> None:
        res = self.service.create_reservation({
            "id": "res-up", "scheduled_at": "2026-11-12T14:30:00+09:00",
            "cleaning_type": CLEANING_TYPE_MOP_AFTER_VACUUM,
        })
        self.assertEqual(res.cleaning_type, CLEANING_TYPE_MOP_AFTER_VACUUM)

        # 유효한 cleaning_type으로 수정
        val_up = validate_reservation_update_payload(
            {"scheduled_at": "2026-11-12T15:00:00+09:00", "cleaning_type": CLEANING_TYPE_VACUUM},
            now_func=lambda: self.fixed_now,
        )
        self.assertEqual(val_up["cleaning_type"], CLEANING_TYPE_VACUUM)

        updated = self.service.update_reservation("res-up", {
            "scheduled_at": "2026-11-12T15:00:00+09:00",
            "cleaning_type": CLEANING_TYPE_VACUUM,
        })
        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.cleaning_type, CLEANING_TYPE_VACUUM)

        # 잘못된 cleaning_type 수정 시도 거부
        with self.assertRaises(ReservationValidationError):
            self.service.update_reservation("res-up", {
                "scheduled_at": "2026-11-12T15:30:00+09:00",
                "cleaning_type": "invalid_mode",
            })


class TestSmartThingsClientCleaningType(unittest.TestCase):
    """SmartThingsClient의 set_cleaning_type 명세 검증."""

    def setUp(self) -> None:
        token = TokenInfo(access_token="test-pat-token", token_type="pat")
        self.mgr = SmartThingsTokenManager(token_info=token)
        self.client = SmartThingsClient(token_manager=self.mgr, simulated=True)

    def test_set_cleaning_type_simulated(self) -> None:
        res = self.client.set_cleaning_type("dev-1", CLEANING_TYPE_VACUUM)
        self.assertEqual(res["results"][0]["status"], "COMPLETED")
        self.assertEqual(self.client.simulated_cleaning_type, CLEANING_TYPE_VACUUM)

    def test_set_cleaning_type_commands_structure(self) -> None:
        # 실제 HTTP 요청 대신 execute_commands mock을 통한 인자 구조 검증
        mock_execute = MagicMock(return_value={"results": [{"status": "COMPLETED"}]})
        real_client = SmartThingsClient(token_manager=self.mgr, simulated=False)
        real_client.execute_commands = mock_execute  # type: ignore[method-assign]

        real_client.set_cleaning_type("dev-001", CLEANING_TYPE_VACUUM_AND_MOP)
        mock_execute.assert_called_once_with("dev-001", [{
            "component": "main",
            "capability": "samsungce.robotCleanerCleaningType",
            "command": "setCleaningType",
            "arguments": [CLEANING_TYPE_VACUUM_AND_MOP],
        }])

    def test_set_cleaning_type_invalid_value_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.client.set_cleaning_type("dev-1", "turboClean")


class TestSchedulerCleaningTypeExecution(unittest.TestCase):
    """스케줄러 예약 실행 시 set_cleaning_type 호출 및 vacuum 물걸레 횟수 제외 검증."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.storage = Storage(Path(self.temp_dir.name) / "sched.db")
        token = TokenInfo(access_token="test-token", token_type="pat")
        self.client = SmartThingsClient(token_manager=SmartThingsTokenManager(token_info=token), simulated=True)
        self.scheduler = ReservationScheduler(
            storage=self.storage, client=self.client, device_id="dev-jetbot", poll_interval=0.05,
        )

    def tearDown(self) -> None:
        self.scheduler.stop()
        self.storage.close()
        self.temp_dir.cleanup()

    def test_scheduler_calls_set_cleaning_type_then_start(self) -> None:
        calls: list[tuple[str, Any]] = []

        def fake_set_ct(d: str, c: str) -> dict[str, Any]:
            calls.append(("set_cleaning_type", (d, c)))
            return {"results": []}

        def fake_start(d: str) -> dict[str, Any]:
            calls.append(("start_cleaning", (d,)))
            return {"results": []}

        self.client.set_cleaning_type = fake_set_ct  # type: ignore[assignment]
        self.client.start_cleaning = fake_start  # type: ignore[assignment]

        res_time = datetime(2026, 11, 12, 16, 0, tzinfo=KST)
        self.storage.save_reservation(Reservation(
            id="res-sched-1", reserved_at=res_time, status=STATUS_SCHEDULED, cleaning_type=CLEANING_TYPE_VACUUM,
        ))
        executed = self.scheduler.step(now=res_time + timedelta(seconds=1))
        self.assertEqual(len(executed), 1)
        self.assertEqual(calls, [
            ("set_cleaning_type", ("dev-jetbot", CLEANING_TYPE_VACUUM)),
            ("start_cleaning", ("dev-jetbot",)),
        ])

    def test_vacuum_does_not_increment_mop_clean_count(self) -> None:
        self.client.get_device_status = lambda d: DeviceStatus(device_id=d, operating_state="cleaning")  # type: ignore[assignment]
        water = self.scheduler.water_service
        assert water is not None
        water.refill_water()

        res_time = datetime(2026, 11, 12, 16, 0, tzinfo=KST)
        self.storage.save_reservation(Reservation(
            id="res-vac", reserved_at=res_time, status=STATUS_SCHEDULED,
            cleaning_type=CLEANING_TYPE_VACUUM, executed_at=res_time,
        ))
        self.scheduler.step(now=res_time + timedelta(minutes=1))
        res = self.storage.get_reservation("res-vac")
        assert res is not None
        self.assertEqual(res.status, STATUS_SUCCESS)
        self.assertEqual(water.get_tracker().mop_clean_count, 0)

    def test_mop_and_combinations_increment_mop_clean_count(self) -> None:
        self.client.get_device_status = lambda d: DeviceStatus(device_id=d, operating_state="cleaning")  # type: ignore[assignment]
        water = self.scheduler.water_service
        assert water is not None
        water.refill_water()

        mop_types = [CLEANING_TYPE_MOP, CLEANING_TYPE_VACUUM_AND_MOP, CLEANING_TYPE_MOP_AFTER_VACUUM]
        for idx, ctype in enumerate(mop_types):
            res_time = datetime(2026, 11, 12, 17, idx, tzinfo=KST)
            self.storage.save_reservation(Reservation(
                id=f"res-mop-{idx}", reserved_at=res_time, status=STATUS_SCHEDULED,
                cleaning_type=ctype, executed_at=res_time,
            ))
            self.scheduler.step(now=res_time + timedelta(minutes=1))
            res = self.storage.get_reservation(f"res-mop-{idx}")
            assert res is not None
            self.assertEqual(res.status, STATUS_SUCCESS)
            self.assertEqual(water.get_tracker().mop_clean_count, idx + 1)


if __name__ == "__main__":
    unittest.main()
