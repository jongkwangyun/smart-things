from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from server.smartthings.auth import SmartThingsTokenManager
from server.time_utils import format_kst, now_kst

SUPPORTED_OPERATING_STATES: list[str] = [
    "homing", "charging", "charged", "chargingForRemainingJob", "moving",
    "cleaning", "paused", "idle", "error", "powerSaving", "factoryReset",
    "relocal", "exploring", "patrol", "monitoring", "manual", "processing",
    "messaging", "emitDust", "findingPet", "calibrating", "welcoming",
    "monitoringAutomation", "washingMop", "sterilizingMop", "dryingMop",
    "supplyingWater", "preparingWater", "spinDrying", "flexCharged",
    "mopWashingPaused", "descaling", "drainingWater", "waitingForDescaling",
    "sabbath",
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
    raw_status: dict[str, Any] = field(default_factory=dict)
    checked_at: str = ""

    def summary(self) -> str:
        return (
            f"기기 ID: {self.device_id} | 모델: {self.model} | "
            f"상태: {self.operating_state} | 배터리: {self.battery}% | "
            f"전원: {self.switch} | 온라인: {self.is_online}"
        )


def make_simulated_status(device_id: str, operating_state: str = "idle", battery: int = 100) -> dict[str, Any]:
    """오프라인/테스트 환경용 VR7MD97716G 시뮬레이션 상태 딕셔너리 생성."""
    return {
        "components": {
            "main": {
                "robotCleanerOperatingState": {
                    "operatingState": {"value": operating_state}
                },
                "robotCleanerMovement": {
                    "homing": {"value": False}
                },
                "battery": {
                    "battery": {"value": battery, "unit": "%"}
                },
                "switch": {
                    "switch": {"value": "on" if operating_state == "cleaning" else "off"}
                },
                "custom.dustFilter": {
                    "filterStatus": {"value": "normal"}
                },
            }
        }
    }


class SmartThingsClient:
    """스마트싱스 REST API 통신 클라이언트."""

    def __init__(
        self,
        token_manager: SmartThingsTokenManager,
        base_url: str = "https://api.smartthings.com/v1",
        simulated: bool = False,
    ) -> None:
        self.token_manager = token_manager
        self.base_url = base_url.rstrip("/")
        self.simulated = simulated

    def get_device_status(self, device_id: str) -> DeviceStatus:
        """기기 상태를 조회한다 (/v1/devices/{device_id}/status).

        401 Unauthorized 발생 시 자동으로 토큰을 갱신하고 1회 재시도한다.
        """
        token = self.token_manager.get_token()

        if self.simulated:
            raw = make_simulated_status(device_id)
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
                "deviceId": device_id,
                "name": "VR7MD97716G",
                "label": "비스포크 AI 콤보 로봇청소기",
                "deviceManufacturerCode": "Samsung Electronics",
                "deviceModel": "JETBOT_COMBOT_9X00_24K",
            }
        token = self.token_manager.get_token()
        return self._request_json(f"{self.base_url}/devices/{device_id}", token)

    def execute_commands(self, device_id: str, commands: list[dict[str, Any]]) -> dict[str, Any]:
        """기기 명령(/v1/devices/{device_id}/commands)을 전송한다.

        401 Unauthorized 발생 시 자동으로 토큰을 갱신하고 1회 재시도한다.
        """
        if self.simulated:
            self.token_manager.logger.info(
                "기기 명령 전송 성공 (시뮬레이션 모드): device_id=%s, commands=%s", device_id, commands
            )
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

    def start_cleaning(self, device_id: str) -> dict[str, Any]:
        """청소 시작 명령을 전송한다 (samsungce.robotCleanerOperatingState: start)."""
        cmd = [{
            "component": "main",
            "capability": "samsungce.robotCleanerOperatingState",
            "command": "start",
            "arguments": [],
        }]
        return self.execute_commands(device_id, cmd)

    def return_to_home(self, device_id: str) -> dict[str, Any]:
        """청소 중단 및 충전 스테이션 복귀 명령을 전송한다 (samsungce.robotCleanerOperatingState: returnToHome)."""
        cmd = [{
            "component": "main",
            "capability": "samsungce.robotCleanerOperatingState",
            "command": "returnToHome",
            "arguments": [],
        }]
        return self.execute_commands(device_id, cmd)

    def stop_cleaning(self, device_id: str) -> dict[str, Any]:
        """청소 정지/복귀 명령 (return_to_home 호출)."""
        return self.return_to_home(device_id)

    def _request_json(
        self,
        url: str,
        token: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
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

        op_state = (
            main.get("robotCleanerOperatingState", {})
            .get("operatingState", {})
            .get("value", "idle")
        )
        battery_val = (
            main.get("battery", {})
            .get("battery", {})
            .get("value", 100)
        )
        try:
            battery = int(battery_val)
        except (ValueError, TypeError):
            battery = 100

        switch_val = (
            main.get("switch", {})
            .get("switch", {})
            .get("value", "off")
        )
        filter_val = (
            main.get("custom.dustFilter", {})
            .get("filterStatus", {})
            .get("value", "normal")
        )

        return DeviceStatus(
            device_id=device_id,
            model="VR7MD97716G",
            name="Bespoke AI Combo",
            operating_state=str(op_state),
            battery=battery,
            is_online=True,
            switch=str(switch_val),
            dust_filter_status=str(filter_val),
            raw_status=data,
            checked_at=format_kst(now_kst()),
        )
