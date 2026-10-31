from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.time_utils import format_kst, now_kst, parse_kst

STATUS_SCHEDULED = "예정"
STATUS_SUCCESS = "성공"
STATUS_FAILED = "실패"
STATUS_CANCELLED = "취소"
STATUS_MISSED = "누락"

VALID_RESERVATION_STATUSES = {
    STATUS_SCHEDULED,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_MISSED,
}


@dataclass
class Reservation:
    id: str
    reserved_at: datetime
    status: str = STATUS_SCHEDULED
    allow_during_off_period: bool = False
    failure_reason: str | None = None
    last_device_state: str | None = None
    created_at: datetime = field(default_factory=now_kst)
    updated_at: datetime = field(default_factory=now_kst)
    executed_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "reserved_at": format_kst(self.reserved_at),
            "scheduled_at": format_kst(self.reserved_at),
            "status": self.status,
            "allow_during_off_period": self.allow_during_off_period,
            "failure_reason": self.failure_reason,
            "last_device_state": self.last_device_state,
            "created_at": format_kst(self.created_at),
            "updated_at": format_kst(self.updated_at),
            "executed_at": format_kst(self.executed_at) if self.executed_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Reservation:
        raw_at = data.get("scheduled_at") or data["reserved_at"]
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            reserved_at=parse_kst(raw_at),
            status=str(data.get("status", STATUS_SCHEDULED)),
            allow_during_off_period=bool(data.get("allow_during_off_period", False)),
            failure_reason=data.get("failure_reason"),
            last_device_state=data.get("last_device_state"),
            created_at=parse_kst(data["created_at"]) if "created_at" in data else now_kst(),
            updated_at=parse_kst(data["updated_at"]) if "updated_at" in data else now_kst(),
            executed_at=parse_kst(data["executed_at"]) if data.get("executed_at") else None,
        )


@dataclass
class OffPeriod:
    id: str
    start_at: datetime
    end_at: datetime
    created_at: datetime = field(default_factory=now_kst)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start_at": format_kst(self.start_at),
            "end_at": format_kst(self.end_at),
            "created_at": format_kst(self.created_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OffPeriod:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            start_at=parse_kst(data["start_at"]),
            end_at=parse_kst(data["end_at"]),
            created_at=parse_kst(data["created_at"]) if "created_at" in data else now_kst(),
        )


@dataclass
class AutoStopEvent:
    id: str
    occurred_at: datetime
    off_period_id: str
    success: bool = True
    details: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "occurred_at": format_kst(self.occurred_at),
            "off_period_id": self.off_period_id,
            "success": self.success,
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutoStopEvent:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            occurred_at=parse_kst(data["occurred_at"]) if "occurred_at" in data else now_kst(),
            off_period_id=str(data["off_period_id"]),
            success=bool(data.get("success", True)),
            details=data.get("details"),
        )


@dataclass
class WaterTracker:
    id: str = "default"
    last_refilled_at: datetime | None = None
    mop_clean_count: int = 0
    alert_threshold_n: int = 5
    last_alerted_at: datetime | None = None
    updated_at: datetime = field(default_factory=now_kst)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "last_refilled_at": format_kst(self.last_refilled_at) if self.last_refilled_at else None,
            "mop_clean_count": self.mop_clean_count,
            "alert_threshold_n": self.alert_threshold_n,
            "last_alerted_at": format_kst(self.last_alerted_at) if self.last_alerted_at else None,
            "updated_at": format_kst(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WaterTracker:
        return cls(
            id=str(data.get("id", "default")),
            last_refilled_at=parse_kst(data["last_refilled_at"]) if data.get("last_refilled_at") else None,
            mop_clean_count=int(data.get("mop_clean_count", 0)),
            alert_threshold_n=int(data.get("alert_threshold_n", 5)),
            last_alerted_at=parse_kst(data["last_alerted_at"]) if data.get("last_alerted_at") else None,
            updated_at=parse_kst(data["updated_at"]) if "updated_at" in data else now_kst(),
        )


@dataclass
class NotificationTarget:
    id: str
    device_id: str
    push_token: str
    updated_at: datetime = field(default_factory=now_kst)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "device_id": self.device_id,
            "push_token": self.push_token,
            "updated_at": format_kst(self.updated_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NotificationTarget:
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:8]),
            device_id=str(data["device_id"]),
            push_token=str(data["push_token"]),
            updated_at=parse_kst(data["updated_at"]) if "updated_at" in data else now_kst(),
        )
