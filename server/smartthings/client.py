from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from server.smartthings.auth import SmartThingsTokenManager
from server.time_utils import format_kst, now_kst

SUPPORTED_OPERATING_STATES: list[str] = [
    "homing", "charging", "charged", "chargingForRemainingJob", "moving", "cleaning", "paused", "idle", "error",
    "powerSaving", "factoryReset", "relocal", "exploring", "patrol", "monitoring", "manual", "processing",
    "messaging", "emitDust", "findingPet", "calibrating", "welcoming", "monitoringAutomation", "washingMop",
    "sterilizingMop", "dryingMop", "supplyingWater", "preparingWater", "spinDrying", "flexCharged",
    "mopWashingPaused", "descaling", "drainingWater", "waitingForDescaling", "sabbath",
]


class SmartThingsError(Exception):
    """스마트싱스 연동 기본 예외."""


class SmartThingsAuthError(SmartThingsError):
    """스마트싱스 인증/권한 실패 예외 (HTTP 401 등)."""


class SmartThingsApiError(SmartThingsError):
    """스마트싱스 API 호출/응답 실패 예외."""


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


class SmartThingsClient:
    """스마트싱스 REST API 통신 클라이언트."""

    def __init__(
        self, token_manager: SmartThingsTokenManager, base_url: str = "https://api.smartthings.com/v1",
        simulated: bool = False,
    ) -> None:
        self.token_manager = token_manager
        self.base_url = base_url.rstrip("/")
        self.simulated = simulated
        self.simulated_health = "ONLINE"
        self.simulated_dnd: dict[str, str] = {"startTime": "0000", "endTime": "0000", "doNotDisturb": "off"}

    def get_device_status(self, device_id: str) -> DeviceStatus:
        """기기 상태를 조회한다 (/v1/devices/{device_id}/status).

        401 Unauthorized 발생 시 자동으로 토큰을 갱신하고 1회 재시도한다.
        """
        token = self.token_manager.get_token()

        if self.simulated:
            dnd = getattr(self, "simulated_dnd", {"startTime": "0000", "endTime": "0000", "doNotDisturb": "off"})
            raw = make_simulated_status(
                device_id, dnd_mode=dnd.get("doNotDisturb", "off"),
                dnd_start=dnd.get("startTime", "0000"), dnd_end=dnd.get("endTime", "0000"),
            )
            self.token_manager.logger.info("기기 상태 조회 성공 (시뮬레이션 모드): device_id=%s", device_id)
            return self._parse_status(device_id, raw)

        try:
            raw = self._request_json(f"{self.base_url}/devices/{device_id}/status", token)
            self.token_manager.logger.info("기기 상태 조회 성공 (실제 API): device_id=%s", device_id)
        except SmartThingsAuthError:
            self.token_manager.logger.warning("401 Unauthorized 발생. 토큰 갱신 후 1회 재시도합니다.")
            self.token_manager.refresh(reason="401_unauthorized")
            new_token = self.token_manager.get_token()
            raw = self._request_json(f"{self.base_url}/devices/{device_id}/status", new_token)
            self.token_manager.logger.info("재시도 후 기기 상태 조회 성공: device_id=%s", device_id)

        return self._parse_status(device_id, raw)

    def get_device_info(self, device_id: str) -> dict[str, Any]:
        """기기 기본 정보(/v1/devices/{device_id})를 조회한다."""
        if self.simulated:
            return {
                "deviceId": device_id, "name": "VR7MD97716G", "label": "비스포크 AI 콤보 로봇청소기",
                "deviceManufacturerCode": "Samsung Electronics", "deviceModel": "JETBOT_COMBOT_9X00_24K",
            }
        token = self.token_manager.get_token()
        return self._request_json(f"{self.base_url}/devices/{device_id}", token)

    def execute_commands(self, device_id: str, commands: list[dict[str, Any]]) -> dict[str, Any]:
        """기기 명령(/v1/devices/{device_id}/commands)을 전송한다.

        401 Unauthorized 발생 시 자동으로 토큰을 갱신하고 1회 재시도한다.
        """
        if self.simulated:
            self.token_manager.logger.info("기기 명령 성공 (시뮬): device_id=%s, cmds=%s", device_id, commands)
            return {"results": [{"id": "simulated-cmd-id", "status": "COMPLETED"}]}

        url = f"{self.base_url}/devices/{device_id}/commands"
        payload = {"commands": commands}
        token = self.token_manager.get_token()

        try:
            res = self._request_json(url, token, method="POST", payload=payload)
            self.token_manager.logger.info("기기 명령 전송 성공 (실제 API): device_id=%s", device_id)
            return res
        except SmartThingsAuthError:
            self.token_manager.logger.warning("401 Unauthorized 발생. 토큰 갱신 후 명령 1회 재시도합니다.")
            self.token_manager.refresh(reason="401_unauthorized")
            new_token = self.token_manager.get_token()
            res = self._request_json(url, new_token, method="POST", payload=payload)
            self.token_manager.logger.info("재시도 후 기기 명령 전송 성공: device_id=%s", device_id)
            return res

    def get_device_health(self, device_id: str) -> str:
        """기기 헬스 상태(/v1/devices/{device_id}/health)를 조회한다 ("ONLINE", "OFFLINE", "UNKNOWN")."""
        if self.simulated:
            return getattr(self, "simulated_health", "ONLINE")

        url = f"{self.base_url}/devices/{device_id}/health"
        token = self.token_manager.get_token()
        try:
            raw = self._request_json(url, token)
        except SmartThingsAuthError:
            self.token_manager.logger.warning("401 발생. 토큰 갱신 후 헬스 조회 1회 재시도.")
            self.token_manager.refresh(reason="401_unauthorized")
            raw = self._request_json(url, self.token_manager.get_token())
        except SmartThingsApiError:
            return "UNKNOWN"

        state = raw.get("state")
        return str(state).upper() if isinstance(state, str) else "UNKNOWN"

    def parse_failure_reason(
        self, device_status: DeviceStatus | None, health_state: str = "ONLINE", fallback_unknown: bool = False,
    ) -> str | None:
        """기기 상태 및 헬스 정보로부터 청소 실패 원인을 분석한다 (REQ-009, A-03)."""
        return parse_failure_reason(device_status, health_state, fallback_unknown=fallback_unknown)

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        """청소 시작 명령을 전송한다 (samsungce.robotCleanerOperatingState: start)."""
        return self.execute_commands(device_id, [{
            "component": "main", "capability": "samsungce.robotCleanerOperatingState",
            "command": "start", "arguments": [],
        }])

    def return_to_home(self, device_id: str) -> dict[str, Any]:
        """청소 중단 및 충전 스테이션 복귀 명령을 전송한다 (samsungce.robotCleanerOperatingState: returnToHome)."""
        return self.execute_commands(device_id, [{
            "component": "main", "capability": "samsungce.robotCleanerOperatingState",
            "command": "returnToHome", "arguments": [],
        }])

    def stop_cleaning(self, device_id: str) -> dict[str, Any]:
        """청소 정지/복귀 명령 (return_to_home 호출)."""
        return self.return_to_home(device_id)

    def get_dnd_status(self, device_id: str) -> dict[str, str]:
        """기기 DND 설정 상태를 조회한다 (custom.doNotDisturbMode)."""
        if self.simulated:
            dnd = getattr(self, "simulated_dnd", {"startTime": "0000", "endTime": "0000", "doNotDisturb": "off"})
            return dict(dnd)
        st = self.get_device_status(device_id)
        dnd = st.raw_status.get("components", {}).get("main", {}).get("custom.doNotDisturbMode", {})
        return {
            "startTime": str(dnd.get("startTime", {}).get("value", "0000")),
            "endTime": str(dnd.get("endTime", {}).get("value", "0000")),
            "doNotDisturb": str(dnd.get("doNotDisturb", {}).get("value", "off")),
        }

    def set_dnd_mode(self, device_id: str, start_time: str, end_time: str, mode: str) -> dict[str, Any]:
        """기기 DND 설정을 변경한다 (custom.doNotDisturbMode: setDoNotDisturbMode)."""
        cmd = [{
            "component": "main", "capability": "custom.doNotDisturbMode",
            "command": "setDoNotDisturbMode", "arguments": [str(start_time), str(end_time), str(mode)],
        }]
        if self.simulated:
            self.simulated_dnd = {"startTime": str(start_time), "endTime": str(end_time), "doNotDisturb": str(mode)}
        return self.execute_commands(device_id, cmd)

    def _request_json(
        self, url: str, token: str, method: str = "GET", payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SmartThingsAuthError(f"스마트싱스 인증 실패 (HTTP 401): {e.reason}") from e
            body_text = e.read().decode("utf-8", errors="replace")
            raise SmartThingsApiError(f"스마트싱스 API 오류 (HTTP {e.code}): {body_text}") from e
        except urllib.error.URLError as e:
            raise SmartThingsApiError(f"스마트싱스 네트워크 연결 실패: {e.reason}") from e
        except Exception as e:
            raise SmartThingsApiError(f"스마트싱스 요청 실패: {e}") from e

    def _parse_status(self, device_id: str, data: dict[str, Any]) -> DeviceStatus:
        components = data.get("components", {})
        main = components.get("main", {})
        station = components.get("station", {})

        op_state = main.get("robotCleanerOperatingState", {}).get("operatingState", {}).get("value", "idle")
        try:
            battery = int(main.get("battery", {}).get("battery", {}).get("value", 100))
        except (ValueError, TypeError):
            battery = 100

        switch_val = main.get("switch", {}).get("switch", {}).get("value", "off")
        filter_val = main.get("custom.dustFilter", {}).get("filterStatus", {}).get("value", "normal")
        dust_bag = (
            station.get("samsungce.robotCleanerDustBag", {}).get("dustBagStatus", {}).get("value")
            or station.get("samsungce.robotCleanerDustBag", {}).get("status", {}).get("value", "normal")
        )
        motor_filter = (
            main.get("samsungce.robotCleanerMotorFilter", {}).get("motorFilterStatus", {}).get("value", "normal")
        )

        return DeviceStatus(
            device_id=device_id, model="VR7MD97716G", name="Bespoke AI Combo",
            operating_state=str(op_state), battery=battery, is_online=True,
            switch=str(switch_val), dust_filter_status=str(filter_val),
            dust_bag_status=str(dust_bag), motor_filter_status=str(motor_filter),
            raw_status=data, checked_at=format_kst(now_kst()),
        )

