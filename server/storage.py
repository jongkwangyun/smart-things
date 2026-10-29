from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from server.models import (
    AutoStopEvent,
    NotificationTarget,
    OffPeriod,
    Reservation,
    WaterTracker,
)
from server.schema import INIT_SCHEMA_SQL
from server.time_utils import format_kst, now_kst, parse_kst


class Storage:
    """미니PC 로컬 SQLite 영구 저장소."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        if self.db_path != Path(":memory:"):
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.executescript(INIT_SCHEMA_SQL)
            self._conn.commit()

    # --- Reservation ---
    def save_reservation(self, res: Reservation) -> Reservation:
        sql = """
            INSERT INTO reservations (
                id, reserved_at, status, allow_during_off_period,
                failure_reason, last_device_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                reserved_at=excluded.reserved_at, status=excluded.status,
                allow_during_off_period=excluded.allow_during_off_period,
                failure_reason=excluded.failure_reason,
                last_device_state=excluded.last_device_state, updated_at=excluded.updated_at;
        """
        vals = (
            res.id,
            format_kst(res.reserved_at),
            res.status,
            1 if res.allow_during_off_period else 0,
            res.failure_reason,
            res.last_device_state,
            format_kst(res.created_at),
            format_kst(res.updated_at),
        )
        with self._lock:
            self._conn.execute(sql, vals)
            self._conn.commit()
            return res

    def get_reservation(self, res_id: str) -> Reservation | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM reservations WHERE id = ?", (res_id,)).fetchone()
            return self._row_to_reservation(row) if row else None

    def list_reservations(self) -> list[Reservation]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM reservations ORDER BY reserved_at ASC").fetchall()
            return [self._row_to_reservation(r) for r in rows]

    def delete_reservation(self, res_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM reservations WHERE id = ?", (res_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def _row_to_reservation(self, row: sqlite3.Row) -> Reservation:
        return Reservation(
            id=row["id"],
            reserved_at=parse_kst(row["reserved_at"]),
            status=row["status"],
            allow_during_off_period=bool(row["allow_during_off_period"]),
            failure_reason=row["failure_reason"],
            last_device_state=row["last_device_state"],
            created_at=parse_kst(row["created_at"]),
            updated_at=parse_kst(row["updated_at"]),
        )

    # --- OffPeriod ---
    def save_off_period(self, op: OffPeriod) -> OffPeriod:
        sql = """
            INSERT INTO off_periods (id, start_at, end_at, created_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET start_at=excluded.start_at, end_at=excluded.end_at;
        """
        vals = (op.id, format_kst(op.start_at), format_kst(op.end_at), format_kst(op.created_at))
        with self._lock:
            self._conn.execute(sql, vals)
            self._conn.commit()
            return op

    def get_off_period(self, op_id: str) -> OffPeriod | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM off_periods WHERE id = ?", (op_id,)).fetchone()
            if not row:
                return None
            return OffPeriod(
                id=row["id"],
                start_at=parse_kst(row["start_at"]),
                end_at=parse_kst(row["end_at"]),
                created_at=parse_kst(row["created_at"]),
            )

    def list_off_periods(self) -> list[OffPeriod]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM off_periods ORDER BY start_at ASC").fetchall()
            return [
                OffPeriod(
                    id=r["id"],
                    start_at=parse_kst(r["start_at"]),
                    end_at=parse_kst(r["end_at"]),
                    created_at=parse_kst(r["created_at"]),
                )
                for r in rows
            ]

    def delete_off_period(self, op_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM off_periods WHERE id = ?", (op_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # --- AutoStopEvent ---
    def save_auto_stop_event(self, event: AutoStopEvent) -> AutoStopEvent:
        sql = """
            INSERT INTO auto_stop_events (id, occurred_at, off_period_id, success, details)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                occurred_at=excluded.occurred_at, off_period_id=excluded.off_period_id,
                success=excluded.success, details=excluded.details;
        """
        vals = (
            event.id,
            format_kst(event.occurred_at),
            event.off_period_id,
            1 if event.success else 0,
            event.details,
        )
        with self._lock:
            self._conn.execute(sql, vals)
            self._conn.commit()
            return event

    def list_auto_stop_events(self) -> list[AutoStopEvent]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM auto_stop_events ORDER BY occurred_at DESC").fetchall()
            return [
                AutoStopEvent(
                    id=r["id"],
                    occurred_at=parse_kst(r["occurred_at"]),
                    off_period_id=r["off_period_id"],
                    success=bool(r["success"]),
                    details=r["details"],
                )
                for r in rows
            ]

    # --- WaterTracker ---
    def get_water_tracker(self, tracker_id: str = "default") -> WaterTracker:
        with self._lock:
            row = self._conn.execute("SELECT * FROM water_trackers WHERE id = ?", (tracker_id,)).fetchone()
            if not row:
                return self.save_water_tracker(WaterTracker(id=tracker_id, updated_at=now_kst()))
            return WaterTracker(
                id=row["id"],
                last_refilled_at=parse_kst(row["last_refilled_at"]) if row["last_refilled_at"] else None,
                mop_clean_count=row["mop_clean_count"],
                alert_threshold_n=row["alert_threshold_n"],
                last_alerted_at=parse_kst(row["last_alerted_at"]) if row["last_alerted_at"] else None,
                updated_at=parse_kst(row["updated_at"]),
            )

    def save_water_tracker(self, tracker: WaterTracker) -> WaterTracker:
        sql = """
            INSERT INTO water_trackers (
                id, last_refilled_at, mop_clean_count, alert_threshold_n, last_alerted_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                last_refilled_at=excluded.last_refilled_at, mop_clean_count=excluded.mop_clean_count,
                alert_threshold_n=excluded.alert_threshold_n, last_alerted_at=excluded.last_alerted_at,
                updated_at=excluded.updated_at;
        """
        vals = (
            tracker.id,
            format_kst(tracker.last_refilled_at) if tracker.last_refilled_at else None,
            tracker.mop_clean_count,
            tracker.alert_threshold_n,
            format_kst(tracker.last_alerted_at) if tracker.last_alerted_at else None,
            format_kst(tracker.updated_at),
        )
        with self._lock:
            self._conn.execute(sql, vals)
            self._conn.commit()
            return tracker

    # --- NotificationTarget ---
    def save_notification_target(self, target: NotificationTarget) -> NotificationTarget:
        sql = """
            INSERT INTO notification_targets (id, device_id, push_token, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(device_id) DO UPDATE SET push_token=excluded.push_token, updated_at=excluded.updated_at;
        """
        vals = (target.id, target.device_id, target.push_token, format_kst(target.updated_at))
        with self._lock:
            self._conn.execute(sql, vals)
            self._conn.commit()
            return target

    def get_notification_target(self, device_id: str) -> NotificationTarget | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM notification_targets WHERE device_id = ?", (device_id,)).fetchone()
            if not row:
                return None
            return NotificationTarget(
                id=row["id"],
                device_id=row["device_id"],
                push_token=row["push_token"],
                updated_at=parse_kst(row["updated_at"]),
            )

    def list_notification_targets(self) -> list[NotificationTarget]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM notification_targets ORDER BY updated_at ASC").fetchall()
            return [
                NotificationTarget(
                    id=r["id"],
                    device_id=r["device_id"],
                    push_token=r["push_token"],
                    updated_at=parse_kst(r["updated_at"]),
                )
                for r in rows
            ]

    def delete_notification_target(self, device_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM notification_targets WHERE device_id = ?", (device_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            if getattr(self, "_closed", False):
                return
            try:
                self._conn.commit()
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except Exception:
                pass
            finally:
                self._conn.close()
                self._closed = True
