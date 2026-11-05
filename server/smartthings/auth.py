from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from server.smartthings.token_storage import (
    DEFAULT_TOKEN_FILE,
    TokenInfo,
    TokenType,
    load_token_from_file,
    mask_token,
    save_token_to_file,
    sync_token_from_cli,
)
from server.time_utils import KST, format_kst, now_kst

# 하위 호환성을 위한 re-export
__all__ = [
    "DEFAULT_TOKEN_FILE",
    "KSTFormatter",
    "RefreshCallback",
    "SmartThingsTokenManager",
    "TokenInfo",
    "TokenRefreshError",
    "TokenType",
    "get_smartthings_logger",
    "load_token_from_file",
    "mask_token",
    "parse_env_file",
    "resolve_credential",
    "save_token_to_file",
]


class KSTFormatter(logging.Formatter):
    """KST(한국 표준시) 기준 일시 포맷터."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=KST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat()


def get_smartthings_logger(log_file: Path | str = "server/logs/smartthings.log") -> logging.Logger:
    """스마트싱스 전용 로거를 설정하고 반환한다."""
    logger = logging.getLogger("server.smartthings")
    logger.setLevel(logging.INFO)

    log_path = Path(log_file).resolve()
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == log_path:
            return logger

    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.INFO)
    formatter = KSTFormatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


class TokenRefreshError(Exception):
    """스마트싱스 토큰 갱신 실패 예외."""


def parse_env_file(env_path: Path | str = ".env") -> dict[str, str]:
    """지정된 .env 파일에서 환경 변수를 파싱한다."""
    p = Path(env_path)
    if not p.is_file():
        return {}
    results: dict[str, str] = {}
    try:
        content = p.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            results[k.strip()] = v.strip().strip("'\"")
    except Exception:
        pass
    return results


def resolve_credential(key: str, env_path: Path | str = ".env", default: str = "") -> str:
    """환경 변수 -> .env 파일 순으로 자격 증명을 읽는다."""
    val = os.environ.get(key)
    if val:
        return val.strip()
    file_vars = parse_env_file(env_path)
    return file_vars.get(key, default).strip()


RefreshCallback = Callable[[TokenInfo], TokenInfo]


class SmartThingsTokenManager:
    """스마트싱스 토큰 관리자. 만료 감지, 영구 저장 및 OAuth 자동 갱신을 처리한다."""

    def __init__(
        self,
        token_info: TokenInfo,
        log_file: Path | str = "server/logs/smartthings.log",
        refresh_handler: RefreshCallback | None = None,
        env_path: Path | str = ".env",
        token_file: Path | str | None = None,
    ) -> None:
        self.token_info = token_info
        self.log_file = Path(log_file)
        self.refresh_handler = refresh_handler
        self.env_path = Path(env_path)
        self.token_file = Path(token_file).resolve() if token_file else None
        self.logger = get_smartthings_logger(self.log_file)

    @classmethod
    def from_env(
        cls,
        env_path: Path | str = ".env",
        log_file: Path | str = "server/logs/smartthings.log",
        refresh_handler: RefreshCallback | None = None,
        token_file: Path | str | None = DEFAULT_TOKEN_FILE,
    ) -> SmartThingsTokenManager:
        loaded_token: TokenInfo | None = None
        if token_file:
            loaded_token = load_token_from_file(token_file)

        if loaded_token and loaded_token.access_token:
            if not loaded_token.client_id:
                loaded_token.client_id = resolve_credential("SMARTTHINGS_CLIENT_ID", env_path=env_path) or None
            if not loaded_token.client_secret:
                loaded_token.client_secret = resolve_credential("SMARTTHINGS_CLIENT_SECRET", env_path=env_path) or None
            return cls(
                token_info=loaded_token,
                log_file=log_file,
                refresh_handler=refresh_handler,
                env_path=env_path,
                token_file=token_file,
            )

        token = (
            resolve_credential("SMARTTHINGS_TOKEN", env_path=env_path)
            or resolve_credential("SMARTTHINGS_PAT", env_path=env_path)
            or resolve_credential("SMARTTHINGS_ACCESS_TOKEN", env_path=env_path)
        )
        if not token:
            raise ValueError("스마트싱스 인증 토큰이 설정되지 않았습니다 (SMARTTHINGS_TOKEN).")

        refresh_token = resolve_credential("SMARTTHINGS_REFRESH_TOKEN", env_path=env_path) or None
        token_type: TokenType = "oauth" if refresh_token else "pat"
        client_id = resolve_credential("SMARTTHINGS_CLIENT_ID", env_path=env_path) or None
        client_secret = resolve_credential("SMARTTHINGS_CLIENT_SECRET", env_path=env_path) or None

        token_info = TokenInfo(
            access_token=token,
            token_type=token_type,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
        return cls(
            token_info=token_info,
            log_file=log_file,
            refresh_handler=refresh_handler,
            env_path=env_path,
            token_file=token_file,
        )

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        """현재 토큰 만료 여부를 반환한다 (선제적 감지 지원: buffer_seconds=3600 등)."""
        return self.token_info.is_expired(buffer_seconds)

    def force_expire(self) -> None:
        """토큰을 강제로 만료 상태로 만든다 (검증 및 테스트 목적)."""
        past_time = now_kst() - timedelta(seconds=10)
        self.token_info.expires_at = past_time
        self.logger.info(
            "스마트싱스 토큰 강제 만료 설정 완료: type=%s, expired_at=%s",
            self.token_info.token_type,
            format_kst(past_time),
        )

    def get_token(self, buffer_seconds: int = 60) -> str:
        """유효한 액세스 토큰을 반환한다. 만료 시 자동 갱신을 수행한다."""
        if self.is_expired(buffer_seconds=buffer_seconds):
            self.logger.info(
                "토큰 만료 감지됨 (expires_at=%s), 자동 갱신을 수행합니다.",
                format_kst(self.token_info.expires_at) if self.token_info.expires_at else "None",
            )
            self.refresh(reason="token_expired")
        return self.token_info.access_token

    def refresh(self, reason: str = "manual") -> TokenInfo:
        """토큰을 갱신하고 결과를 smartthings.log 에 기록한다."""
        self.logger.info(
            "토큰 갱신 시작: reason=%s, type=%s, token=%s",
            reason,
            self.token_info.token_type,
            mask_token(self.token_info.access_token),
        )

        try:
            if self.refresh_handler:
                new_info = self.refresh_handler(self.token_info)
            elif self.token_info.token_type == "oauth" and self.token_info.refresh_token:
                new_info = self._refresh_oauth()
            else:
                new_info = self._refresh_from_env_or_extend()
        except Exception as e:
            self.logger.error("토큰 갱신 실패: %s", e)
            raise TokenRefreshError(f"토큰 갱신 실패: {e}") from e

        self.token_info = new_info

        if self.token_file and self.token_info.token_type == "oauth":
            try:
                save_token_to_file(self.token_info, self.token_file)
                self.logger.info("롤링 토큰 파일 영구 저장 완료: %s", self.token_file)
            except Exception as se:
                self.logger.warning("토큰 파일 영구 저장 실패 (%s): %s", self.token_file, se)

        self.logger.info(
            "토큰 갱신 완료: type=%s, token=%s, expires_at=%s",
            self.token_info.token_type,
            mask_token(self.token_info.access_token),
            format_kst(self.token_info.expires_at) if self.token_info.expires_at else "None",
        )
        return self.token_info

    def _refresh_from_env_or_extend(self) -> TokenInfo:
        """PAT 또는 .env 파일에서 토큰을 재로딩하고 유효기간을 연장한다."""
        env_token = (
            resolve_credential("SMARTTHINGS_TOKEN", env_path=self.env_path)
            or resolve_credential("SMARTTHINGS_PAT", env_path=self.env_path)
        )
        active_token = env_token if env_token else self.token_info.access_token
        new_expires_at = now_kst() + timedelta(hours=1)
        return TokenInfo(
            access_token=active_token,
            token_type=self.token_info.token_type,
            refresh_token=self.token_info.refresh_token,
            expires_at=new_expires_at,
            client_id=self.token_info.client_id,
            client_secret=self.token_info.client_secret,
            token_url=self.token_info.token_url,
        )

    def _refresh_oauth(self) -> TokenInfo:
        """OAuth 리프레시 토큰 엔드포인트를 호출하여 토큰을 갱신한다."""
        if not self.token_info.client_id or not self.token_info.refresh_token:
            raise TokenRefreshError("OAuth 토큰 갱신에 필요한 client_id, refresh_token이 없습니다.")

        if not self.token_info.client_secret:
            cli_refreshed = sync_token_from_cli(self.token_file)
            if cli_refreshed:
                return cli_refreshed
            raise TokenRefreshError("OAuth 토큰 갱신에 필요한 client_secret이 없으며 CLI 동기화도 실패했습니다.")

        data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": self.token_info.client_id,
            "client_secret": self.token_info.client_secret,
            "refresh_token": self.token_info.refresh_token,
        }).encode("utf-8")

        req = urllib.request.Request(self.token_info.token_url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            access_token = body["access_token"]
            refresh_token = body.get("refresh_token", self.token_info.refresh_token)
            expires_in = int(body.get("expires_in", 3600))
            expires_at = now_kst() + timedelta(seconds=expires_in)
            new_info = TokenInfo(
                access_token=access_token,
                token_type="oauth",
                refresh_token=refresh_token,
                expires_at=expires_at,
                client_id=self.token_info.client_id,
                client_secret=self.token_info.client_secret,
                token_url=self.token_info.token_url,
            )

            if self.token_file:
                save_token_to_file(new_info, self.token_file)

            return new_info
