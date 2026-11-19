from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from server.scheduler.logger import DEFAULT_SCHEDULER_LOG_FILE
from server.smartthings.auth import SmartThingsTokenManager, TokenInfo, TokenRefreshError, resolve_credential
from server.smartthings.client import SmartThingsAuthError, SmartThingsClient
from server.time_utils import format_kst, to_kst

DEFAULT_DEVICE_ID = "03431570-5acf-f89e-7b39-275ce15d3258"
AUTH_BACKOFF_BASE_SECONDS = 10.0
AUTH_BACKOFF_MAX_SECONDS = 300.0
DEFAULT_AUTH_ALERT_THRESHOLD = 3
DEFAULT_AUTH_ALERT_COOLDOWN_SECONDS = 6 * 3600.0


def resolve_default_client(log_file: Path | str = DEFAULT_SCHEDULER_LOG_FILE) -> SmartThingsClient:
    """기본 스마트싱스 클라이언트를 생성한다. 토큰 부재 시 시뮬레이션 클라이언트로 대체한다."""
    try:
        return SmartThingsClient(token_manager=SmartThingsTokenManager.from_env())
    except Exception:
        dummy_info = TokenInfo(access_token="simulated-scheduler-token", token_type="pat")
        return SmartThingsClient(
            token_manager=SmartThingsTokenManager(token_info=dummy_info, log_file=log_file),
            simulated=True,
        )


def resolve_default_device_id() -> str:
    """설정된 기기 ID를 반환하거나 기본 VR7MD97716G ID를 반환한다."""
    return resolve_credential("SMARTTHINGS_DEVICE_ID") or DEFAULT_DEVICE_ID


def send_failure_notification(
    logger: logging.Logger,
    notification_service: Any,
    reservation_id: str,
    failure_reason: str | None,
) -> None:
    """예약 실행 실패 시 등록된 모든 기기에 푸시 알림을 발송한다."""
    if notification_service is None:
        return
    title, body = "[로봇청소기] 청소 실패", f"예약 청소가 시작되지 않았습니다: {failure_reason}"
    data = {"type": "cleaning_failed", "reservation_id": reservation_id, "failure_reason": failure_reason or ""}
    try:
        send_fn = getattr(notification_service, "send_to_all", None) or getattr(
            notification_service, "send_multicast", None
        )
        if callable(send_fn):
            send_fn(title=title, body=body, data=data)
    except Exception as e:
        logger.error("실패 푸시 알림 발송 중 오류: res_id=%s, error=%s", reservation_id, e)


class AuthBackoffTracker:
    """연속 스마트싱스 인증 실패 시 지수 백오프, 상태 추적 및 쿨다운 기반 푸시 알림 관리."""

    def __init__(
        self,
        logger: logging.Logger,
        base_seconds: float = AUTH_BACKOFF_BASE_SECONDS,
        max_seconds: float = AUTH_BACKOFF_MAX_SECONDS,
        notification_service: Any | None = None,
        alert_threshold: int = DEFAULT_AUTH_ALERT_THRESHOLD,
        alert_cooldown_seconds: float = DEFAULT_AUTH_ALERT_COOLDOWN_SECONDS,
        notification_service_getter: Callable[[], Any] | None = None,
    ) -> None:
        self.logger = logger
        self.base_seconds = max(1.0, float(base_seconds))
        self.max_seconds = max(self.base_seconds, float(max_seconds))
        self.notification_service = notification_service
        self._notification_service_getter = notification_service_getter
        self.alert_threshold = max(1, int(alert_threshold))
        self.alert_cooldown_seconds = max(0.0, float(alert_cooldown_seconds))
        self._lock = threading.RLock()
        self.failure_count: int = 0
        self.backoff_until: datetime | None = None
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None
        self.last_auth_error: str | None = None
        self.last_alert_sent_at: datetime | None = None
        self._api_error_active: bool = False

    @property
    def is_backing_off(self) -> bool:
        with self._lock:
            return self.backoff_until is not None

    def reset(self) -> None:
        """인증 성공 또는 자격 증명 갱신 시 백오프 및 오류 상태를 초기화한다."""
        with self._lock:
            if self.failure_count > 0 or self.backoff_until is not None:
                self.logger.info("스마트싱스 인증 복구 확인: 백오프 해제 (이전 실패 횟수=%d)", self.failure_count)
            self.failure_count = 0
            self.backoff_until = None
            self.last_error = None
            self.last_auth_error = None
            self._api_error_active = False

    def record_success(self, now: datetime) -> None:
        """스마트싱스 API 호출 성공을 기록하고 오류·백오프 상태를 복구한다."""
        with self._lock:
            self.reset()
            self.last_success_at = to_kst(now)

    def record_api_error(self, now: datetime, error: Exception) -> None:
        """인증 외 일반 SmartThings API 오류를 기록한다 (인증 백오프 횟수는 증가시키지 않음)."""
        with self._lock:
            self.last_error = str(error)
            self._api_error_active = True

    def record_failure(self, now: datetime, error: Exception) -> float:
        """인증 실패를 기록하고 지수 백오프 대기 시간 및 연속 실패 푸시 알림을 처리한다."""
        with self._lock:
            now_k = to_kst(now)
            self.failure_count += 1
            self.last_error = str(error)
            self.last_auth_error = str(error)
            self._api_error_active = False
            delay = min(self.max_seconds, self.base_seconds * (2 ** (self.failure_count - 1)))
            self.backoff_until = now_k + timedelta(seconds=delay)
            self.logger.error(
                "스마트싱스 인증 실패로 API 호출 백오프 적용: failures=%d, retry_after=%.1fs, until=%s, error=%s",
                self.failure_count,
                delay,
                format_kst(self.backoff_until),
                error,
            )
            alert_job = self._prepare_auth_alert_locked(now_k, error)
        if alert_job is not None:
            self._dispatch_auth_alert(*alert_job)
        return delay

    def _resolve_notification_service(self) -> Any | None:
        if self._notification_service_getter is not None:
            svc = self._notification_service_getter()
            if svc is not None:
                return svc
        return self.notification_service

    def _prepare_auth_alert_locked(
        self, now_k: datetime, error: Exception
    ) -> tuple[Any, str, str, dict[str, str], datetime, datetime | None, int] | None:
        """self._lock 보유 상태에서 알림 발송 조건을 검사하고 쿨다운 슬롯을 선점한다."""
        if self.failure_count < self.alert_threshold:
            return None
        if self.last_alert_sent_at is not None:
            elapsed = (now_k - self.last_alert_sent_at).total_seconds()
            if elapsed < self.alert_cooldown_seconds:
                self.logger.debug(
                    "스마트싱스 인증 실패 푸시 쿨다운 중 (남은 시간=%.1fs, failures=%d)",
                    self.alert_cooldown_seconds - elapsed,
                    self.failure_count,
                )
                return None
        svc = self._resolve_notification_service()
        send_fn = getattr(svc, "send_to_all", None) or getattr(svc, "send_multicast", None) if svc else None
        if not callable(send_fn):
            return None

        prev_sent_at = self.last_alert_sent_at
        self.last_alert_sent_at = now_k
        failures = self.failure_count
        title = "[로봇청소기] 스마트싱스 인증 오류"
        body = (
            f"스마트싱스 인증이 연속 {failures}회 실패했습니다. "
            f"미니PC에서 스마트싱스 토큰을 재인증해 주세요. ({error})"
        )
        data = {"type": "smartthings_auth_failed", "failure_count": str(failures), "error": str(error)}
        return (send_fn, title, body, data, now_k, prev_sent_at, failures)

    def _restore_alert_slot(self, claimed_at: datetime, prev_sent_at: datetime | None) -> None:
        with self._lock:
            if self.last_alert_sent_at == claimed_at:
                self.last_alert_sent_at = prev_sent_at

    def _dispatch_auth_alert(
        self,
        send_fn: Any,
        title: str,
        body: str,
        data: dict[str, str],
        claimed_at: datetime,
        prev_sent_at: datetime | None,
        failures: int,
    ) -> bool:
        """락을 해제한 상태에서 FCM 푸시 알림을 발송하고 실패/대상없음 시 쿨다운 슬롯을 복원한다."""
        try:
            res = send_fn(title=title, body=body, data=data)
        except Exception as e:
            self._restore_alert_slot(claimed_at, prev_sent_at)
            self.logger.error("스마트싱스 인증 실패 푸시 발송 중 오류: %s", e)
            return False
        total = getattr(res, "total_targets", None)
        if isinstance(total, int) and total == 0:
            self._restore_alert_slot(claimed_at, prev_sent_at)
            return False
        self.logger.warning(
            "스마트싱스 연속 인증 실패 경고 푸시 발송: failures=%d, cooldown=%.0fs",
            failures,
            self.alert_cooldown_seconds,
        )
        return True

    def to_health_dict(self, simulated: bool = False) -> dict[str, Any]:
        """/health 응답에 포함할 스마트싱스 연동 상태 스냅샷을 반환한다."""
        with self._lock:
            last_ok = format_kst(self.last_success_at) if self.last_success_at is not None else None
            if self.failure_count > 0:
                return {
                    "smartthings_ok": False,
                    "smartthings_auth_state": "auth_failed",
                    "last_success_at": last_ok,
                    "last_error": self.last_error,
                }
            if self._api_error_active:
                return {
                    "smartthings_ok": False,
                    "smartthings_auth_state": "api_error",
                    "last_success_at": last_ok,
                    "last_error": self.last_error,
                }
            if simulated:
                state = "simulated"
            elif self.last_success_at is not None:
                state = "ok"
            else:
                state = "unknown"
            return {
                "smartthings_ok": True,
                "smartthings_auth_state": state,
                "last_success_at": last_ok,
                "last_error": self.last_error,
            }

    def should_skip(self, now: datetime, client: Any) -> bool:
        """현재 백오프 중인지 반환한다. 토큰 파일 또는 .env가 갱신되면 즉시 해제한다."""
        with self._lock:
            if self.backoff_until is None:
                return False

            token_mgr = getattr(client, "token_manager", None)
            if token_mgr is not None and hasattr(token_mgr, "reload_if_updated"):
                try:
                    if token_mgr.reload_if_updated():
                        self.reset()
                        self.logger.info("인증 파일(.env/st_token.json) 갱신 감지로 백오프 즉시 해제 후 재시도")
                        return False
                except Exception as e:
                    self.logger.warning("인증 정보 재로딩 확인 중 오류: %s", e)

            return to_kst(now) < self.backoff_until


class StepStatusCache:
    """1회 스케줄러 step() 동안 백오프 상태와 공유 기기 상태를 일관되게 관리한다."""

    def __init__(
        self,
        now: datetime,
        client: Any,
        device_id: str,
        auth_backoff: AuthBackoffTracker,
    ) -> None:
        self.now = now
        self.client = client
        self.device_id = device_id
        self.auth_backoff = auth_backoff
        self.halted: bool = auth_backoff.should_skip(now, client)
        self.attempted: bool = False
        self.status: Any = None

    def halt(self, error: Exception) -> None:
        """인증 실패를 기록하고 현재 step()의 추가 API 호출을 즉시 중단한다."""
        self.halted = True
        self.auth_backoff.record_failure(self.now, error)

    def get_status(self) -> Any:
        """현재 step() 내에서 get_device_status()를 최대 1회만 호출해 결과를 공유한다."""
        if self.attempted or self.halted:
            return self.status
        self.attempted = True
        get_fn = getattr(self.client, "get_device_status", None)
        if not callable(get_fn):
            return None
        try:
            self.status = get_fn(self.device_id)
            self.auth_backoff.record_success(self.now)
        except (SmartThingsAuthError, TokenRefreshError) as e:
            self.halt(e)
        except Exception as e:
            self.auth_backoff.record_api_error(self.now, e)
            self.auth_backoff.logger.warning("기기 상태 조회 실패: device_id=%s, error=%s", self.device_id, e)
        return self.status
