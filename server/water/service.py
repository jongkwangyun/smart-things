from __future__ import annotations

from typing import TYPE_CHECKING

from server.models import WaterTracker
from server.time_utils import now_kst

if TYPE_CHECKING:
    from server.storage import Storage


class WaterService:
    """물걸레 청소 횟수 및 물 부족 알림 관리 서비스 (REQ-019, REQ-020, REQ-021)."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def get_tracker(self, tracker_id: str = "default") -> WaterTracker:
        """현재 물 추적 상태를 반환한다."""
        return self.storage.get_water_tracker(tracker_id)

    def refill_water(self, tracker_id: str = "default") -> WaterTracker:
        """물 채움을 기록하고 물걸레 청소 횟수를 0으로 초기화한다 (REQ-019, D-14)."""
        tracker = self.storage.get_water_tracker(tracker_id)
        now = now_kst()
        tracker.mop_clean_count = 0
        tracker.last_refilled_at = now
        tracker.updated_at = now
        return self.storage.save_water_tracker(tracker)

    def record_mop_clean(self, tracker_id: str = "default") -> WaterTracker:
        """물걸레 청소 1회 수행을 기록한다 (청소 횟수 +1, REQ-020)."""
        tracker = self.storage.get_water_tracker(tracker_id)
        now = now_kst()
        tracker.mop_clean_count += 1
        tracker.updated_at = now
        return self.storage.save_water_tracker(tracker)

    def set_threshold(self, n: int, tracker_id: str = "default") -> WaterTracker:
        """알림 기준 횟수(N)를 설정한다 (REQ-021)."""
        if n <= 0:
            raise ValueError("alert_threshold_n must be positive")
        tracker = self.storage.get_water_tracker(tracker_id)
        tracker.alert_threshold_n = n
        tracker.updated_at = now_kst()
        return self.storage.save_water_tracker(tracker)
