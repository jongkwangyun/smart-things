from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SUPPORTED_OPERATING_STATES: list[str] = [
    "homing", "charging", "charged", "chargingForRemainingJob", "moving", "cleaning", "paused", "idle", "error",
    "powerSaving", "factoryReset", "relocal", "exploring", "patrol", "monitoring", "manual", "processing",
    "messaging", "emitDust", "findingPet", "calibrating", "welcoming", "monitoringAutomation", "washingMop",
    "sterilizingMop", "dryingMop", "supplyingWater", "preparingWater", "spinDrying", "flexCharged",
    "mopWashingPaused", "descaling", "drainingWater", "waitingForDescaling", "sabbath",
]


@dataclass
class DeviceStatus:
    """스마트싱스 로봇청소기 VR7MD97716G 상태 모델."""
    device_id: str
    model: str = "VR7MD97716G"
    name: str = "Bespoke AI Combo"
    operating_state: str = "idle"
    battery: int = 100
    is_online: bool = True
    switch: str = "off"
    dust_filter_status: str = "normal"
    dust_bag_status: str = "normal"
    motor_filter_status: str = "normal"
    cleaning_type: str = "mopAfterVacuum"
    raw_status: dict[str, Any] = field(default_factory=dict)
    checked_at: str = ""

    def summary(self) -> str:
        return (
            f"기기 ID: {self.device_id} | 모델: {self.model} | 상태: {self.operating_state} | "
            f"배터리: {self.battery}% | 전원: {self.switch} | 온라인: {self.is_online}"
        )


def make_simulated_status(
    device_id: str,
    operating_state: str = "idle",
    battery: int = 100,
    dust_bag_status: str = "normal",
    motor_filter_status: str = "normal",
    dnd_mode: str = "off",
    dnd_start: str = "0000",
    dnd_end: str = "0000",
    cleaning_type: str = "mopAfterVacuum",
) -> dict[str, Any]:
    """오프라인/테스트 환경용 VR7MD97716G 시뮬레이션 상태 딕셔너리 생성."""
    return {
        "components": {
            "main": {
                "robotCleanerOperatingState": {"operatingState": {"value": operating_state}},
                "robotCleanerMovement": {"homing": {"value": False}},
                "battery": {"battery": {"value": battery, "unit": "%"}},
                "switch": {"switch": {"value": "on" if operating_state == "cleaning" else "off"}},
                "custom.dustFilter": {"filterStatus": {"value": "normal"}},
                "samsungce.robotCleanerMotorFilter": {"motorFilterStatus": {"value": motor_filter_status}},
                "samsungce.robotCleanerCleaningType": {
                    "cleaningType": {"value": cleaning_type},
                    "supportedCleaningTypes": {"value": ["vacuum", "mop", "vacuumAndMopTogether", "mopAfterVacuum"]},
                },
                "custom.doNotDisturbMode": {
                    "doNotDisturb": {"value": dnd_mode},
                    "startTime": {"value": dnd_start},
                    "endTime": {"value": dnd_end},
                },
            },
            "station": {"samsungce.robotCleanerDustBag": {"dustBagStatus": {"value": dust_bag_status}}},
        }
    }


def parse_failure_reason(
    device_status: DeviceStatus | None, health_state: str = "ONLINE", fallback_unknown: bool = False,
) -> str | None:
    """기기 상태 및 헬스 정보로부터 청소 실패 원인을 분석한다 (REQ-009, A-03)."""
    if str(health_state).upper() == "OFFLINE" or (device_status is not None and not device_status.is_online):
        return "기기 오프라인"
    if device_status is None:
        return "원인 미확인 (기기 상태 정보 없음)" if fallback_unknown else None
    if device_status.battery < 20:
        return f"배터리 부족 ({device_status.battery}%)"
    if device_status.dust_bag_status == "full":
        return "먼지통 가득 참"
    if device_status.motor_filter_status in ("washRequired", "washable", "dirty") or (
        device_status.motor_filter_status not in ("normal", "")
    ):
        return "모터 필터 세척 필요"
    if device_status.operating_state == "error":
        return "기기 동작 오류 (error)"
    return f"원인 미확인 (마지막 상태: {device_status.operating_state})" if fallback_unknown else None
