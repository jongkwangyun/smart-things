from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from server.smartthings.auth import SmartThingsTokenManager
from server.time_utils import format_kst, now_kst


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

    def _request_json(self, url: str, token: str) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise SmartThingsAuthError(f"스마트싱스 인증 실패 (HTTP 401): {e.reason}") from e
            body = e.read().decode("utf-8", errors="replace")
            raise SmartThingsApiError(f"스마트싱스 API 오류 (HTTP {e.code}): {body}") from e
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
