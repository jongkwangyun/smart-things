from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from server.off_periods.auto_stop import DEFAULT_DEVICE_ID
from server.time_utils import to_kst

if TYPE_CHECKING:
    from server.models import OffPeriod
    from server.smartthings.client import SmartThingsClient
    from server.storage import Storage


class DndSyncManager:
    """쉬는 기간 중 방해금지 모드(DND) 자동 활성화 및 해제 매니저 (REQ-016, LB-045).

    - 쉬는 기간 시작 시: 기기 DND 현재 설정을 영구 저장소에 백업하고, 종일(0000~2359) DND on 활성화.
    - 쉬는 기간 종료 또는 삭제 시: 영구 저장소에서 백업을 읽어 기기 DND 설정을 원래대로 복원하고 백업 삭제.
    - 장애 방어: API 오류 등 예외 발생 시 로깅 후 안전하게 종료하여 스케줄러 루프 중단을 방지함.
    """

    def __init__(
        self,
        storage: Storage,
        client: SmartThingsClient,
        device_id: str = DEFAULT_DEVICE_ID,
        logger: logging.Logger | None = None,
    ) -> None:
        self.storage = storage
        self.client = client
        self.device_id = device_id
        self.logger = logger or logging.getLogger("server.off_periods.dnd_sync")

    def get_active_off_period(self, now: datetime) -> OffPeriod | None:
        """지정 시각이 포함된 활성 쉬는 기간을 반환한다."""
        now_k = to_kst(now)
        for op in self.storage.list_off_periods():
            if to_kst(op.start_at) <= now_k <= to_kst(op.end_at):
                return op
        return None

    def sync(self, now: datetime) -> bool:
        """현재 시각 기준으로 쉬는 기간과 기기 DND 설정을 동기화한다."""
        if not hasattr(self.client, "get_dnd_status") or not hasattr(self.client, "set_dnd_mode"):
            return False
        try:
            active_op = self.get_active_off_period(now)
            backup: dict[str, Any] | None = self.storage.get_dnd_backup()

            # 1. 활성 쉬는 기간 진입: 기존 DND 백업 후 종일 DND 활성화
            if active_op is not None and backup is None:
                curr = self.client.get_dnd_status(self.device_id)
                self.storage.save_dnd_backup(curr)
                self.logger.info(
                    "쉬는 기간 진입으로 DND 백업 완료 (op_id=%s, backup=%s)",
                    active_op.id, curr,
                )
                curr_mode = str(curr.get("doNotDisturb") or curr.get("mode") or "off").lower()
                curr_start = str(curr.get("startTime") or curr.get("start_time") or "")
                curr_end = str(curr.get("endTime") or curr.get("end_time") or "")
                already_full_day = curr_mode == "on" and curr_start == "0000" and curr_end == "2359"

                if not already_full_day:
                    self.client.set_dnd_mode(self.device_id, "0000", "2359", "on")
                    self.logger.info("기기 방해금지 모드 종일 활성화 완료 (0000-2359, on)")
                return True

            # 2. 쉬는 기간 종료 또는 삭제: 백업된 원래 DND 설정으로 복원
            if active_op is None and backup is not None:
                s_time = str(backup.get("startTime") or backup.get("start_time") or "0000")
                e_time = str(backup.get("endTime") or backup.get("end_time") or "0000")
                mode = str(backup.get("doNotDisturb") or backup.get("mode") or "off")
                self.logger.info("쉬는 기간 종료: 원래 DND 설정으로 복원 시도 (%s-%s, %s)", s_time, e_time, mode)
                self.client.set_dnd_mode(self.device_id, s_time, e_time, mode)
                self.storage.clear_dnd_backup()
                self.logger.info("원래 DND 설정 복원 및 백업 삭제 완료")
                return True

            return False
        except Exception as e:
            self.logger.warning("DND 동기화 처리 중 오류 발생 (안전 무시): %s", e, exc_info=True)
            return False
