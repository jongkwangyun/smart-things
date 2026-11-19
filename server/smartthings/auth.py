from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

from server.logging_utils import KSTFormatter
from server.smartthings.token_storage import (
    DEFAULT_TOKEN_FILE,
    CredTuple,
    TokenInfo,
    TokenType,
    extract_creds,
    get_smartthings_logger,
    load_token_from_file,
    mask_token,
    parse_env_file,
    resolve_credential,
    save_token_to_file,
    sync_token_from_cli,
)
from server.time_utils import format_kst, now_kst

__all__ = [
    "DEFAULT_OAUTH_REFRESH_BUFFER_SECONDS", "DEFAULT_TOKEN_FILE", "KSTFormatter", "RefreshCallback",
    "SmartThingsTokenManager", "TokenInfo", "TokenRefreshError", "TokenType", "get_smartthings_logger",
    "load_token_from_file", "mask_token", "parse_env_file", "resolve_credential", "save_token_to_file",
]

DEFAULT_OAUTH_REFRESH_BUFFER_SECONDS = 1800
RefreshCallback = Callable[[TokenInfo], TokenInfo]


class TokenRefreshError(Exception):
    """스마트싱스 토큰 갱신 실패 예외."""


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
        self._last_env_file_creds: CredTuple = extract_creds(parse_env_file(self.env_path))
        self._last_os_env_creds: CredTuple = extract_creds(os.environ)
        self._last_file_snapshot = self._read_token_file_snapshot()

    def _read_token_file_snapshot(self) -> tuple[str, str | None, TokenType] | None:
        if not self.token_file:
            return None
        loaded = load_token_from_file(self.token_file)
        if not loaded or not loaded.access_token:
            return None
        return (loaded.access_token, loaded.refresh_token, loaded.token_type)

    def _fill_oauth_client_creds(self, info: TokenInfo) -> TokenInfo:
        if not info.client_id:
            info.client_id = resolve_credential("SMARTTHINGS_CLIENT_ID", env_path=self.env_path) or None
        if not info.client_secret:
            info.client_secret = resolve_credential("SMARTTHINGS_CLIENT_SECRET", env_path=self.env_path) or None
        return info

    @classmethod
    def from_env(
        cls,
        env_path: Path | str = ".env",
        log_file: Path | str = "server/logs/smartthings.log",
        refresh_handler: RefreshCallback | None = None,
        token_file: Path | str | None = DEFAULT_TOKEN_FILE,
    ) -> SmartThingsTokenManager:
        loaded_token = load_token_from_file(token_file) if token_file else None
        if loaded_token and loaded_token.access_token:
            mgr = cls(loaded_token, log_file, refresh_handler, env_path, token_file)
            mgr._fill_oauth_client_creds(mgr.token_info)
            return mgr

        token = (
            resolve_credential("SMARTTHINGS_TOKEN", env_path=env_path)
            or resolve_credential("SMARTTHINGS_PAT", env_path=env_path)
            or resolve_credential("SMARTTHINGS_ACCESS_TOKEN", env_path=env_path)
        )
        if not token:
            raise ValueError("스마트싱스 인증 토큰이 설정되지 않았습니다 (SMARTTHINGS_TOKEN).")

        refresh_token = resolve_credential("SMARTTHINGS_REFRESH_TOKEN", env_path=env_path) or None
        token_info = TokenInfo(
            access_token=token,
            token_type="oauth" if refresh_token else "pat",
            refresh_token=refresh_token,
            client_id=resolve_credential("SMARTTHINGS_CLIENT_ID", env_path=env_path) or None,
            client_secret=resolve_credential("SMARTTHINGS_CLIENT_SECRET", env_path=env_path) or None,
        )
        return cls(token_info, log_file, refresh_handler, env_path, token_file)

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        """현재 토큰 만료 여부를 반환한다 (선제적 감지 지원: buffer_seconds=3600 등)."""
        return self.token_info.is_expired(buffer_seconds)

    def force_expire(self) -> None:
        """토큰을 강제로 만료 상태로 만든다 (검증 및 테스트 목적)."""
        past_time = now_kst() - timedelta(seconds=10)
        self.token_info.expires_at = past_time
        self.logger.info(
            "스마트싱스 토큰 강제 만료 설정 완료: type=%s, expired_at=%s",
            self.token_info.token_type, format_kst(past_time),
        )

    def reload_if_updated(self) -> bool:
        """st_token.json 또는 .env 파일이 외부에서 갱신되었으면 즉시 토큰을 다시 로드한다."""
        if self.token_file and (loaded := load_token_from_file(self.token_file)) and loaded.access_token:
            snap = (loaded.access_token, loaded.refresh_token, loaded.token_type)
            if snap != self._last_file_snapshot and (
                loaded.access_token != self.token_info.access_token
                or loaded.refresh_token != self.token_info.refresh_token
            ):
                self.token_info = self._fill_oauth_client_creds(loaded)
                self._last_file_snapshot = snap
                self.logger.info(
                    "토큰 파일(%s) 변경 감지로 인증 정보 재로딩 완료: type=%s, token=%s",
                    self.token_file, loaded.token_type, mask_token(loaded.access_token),
                )
                return True

        cur_file_env = extract_creds(parse_env_file(self.env_path))
        if cur_file_env != self._last_env_file_creds:
            self._last_env_file_creds = cur_file_env
            if self._apply_env_creds(cur_file_env, f".env({self.env_path})"):
                return True

        cur_os_env = extract_creds(os.environ)
        if cur_os_env != self._last_os_env_creds:
            self._last_os_env_creds = cur_os_env
            if self._apply_env_creds(cur_os_env, "환경 변수"):
                return True

        return False

    def _apply_env_creds(self, creds: CredTuple, source: str) -> bool:
        tok, ref, cid, csec = creds
        if not tok:
            return False
        if tok == self.token_info.access_token and ref == self.token_info.refresh_token:
            return False
        self.token_info = TokenInfo(
            access_token=tok,
            token_type="oauth" if ref else "pat",
            refresh_token=ref,
            client_id=cid or self.token_info.client_id,
            client_secret=csec or self.token_info.client_secret,
            token_url=self.token_info.token_url,
        )
        self.logger.info("%s 변경 감지로 인증 정보 재로딩 완료: token=%s", source, mask_token(tok))
        return True

    def _effective_buffer_seconds(self, buffer_seconds: int | None = None) -> int:
        if buffer_seconds is not None:
            return buffer_seconds
        return DEFAULT_OAUTH_REFRESH_BUFFER_SECONDS if self.token_info.token_type == "oauth" else 60

    def get_token(self, buffer_seconds: int | None = None) -> str:
        """유효한 액세스 토큰을 반환한다. 만료 시 자동 갱신을 수행한다."""
        if self.is_expired(buffer_seconds=self._effective_buffer_seconds(buffer_seconds)):
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
            reason, self.token_info.token_type, mask_token(self.token_info.access_token),
        )
        try:
            if self.refresh_handler:
                new_info = self.refresh_handler(self.token_info)
            elif self.reload_if_updated():
                exp_oauth = (
                    self.token_info.token_type == "oauth"
                    and self.token_info.refresh_token
                    and self.is_expired(buffer_seconds=self._effective_buffer_seconds())
                )
                new_info = self._refresh_oauth(reason=reason) if exp_oauth else self.token_info
            elif self.token_info.token_type == "oauth" and self.token_info.refresh_token:
                new_info = self._refresh_oauth(reason=reason)
            else:
                new_info = self._refresh_from_env_or_extend(reason=reason)
        except TokenRefreshError as e:
            self.logger.error("토큰 갱신 실패: %s", e)
            raise
        except Exception as e:
            self.logger.error("토큰 갱신 실패: %s", e)
            raise TokenRefreshError(f"토큰 갱신 실패: {e}") from e

        self.token_info = new_info
        if self.token_file and self.token_info.token_type == "oauth":
            try:
                save_token_to_file(self.token_info, self.token_file)
                self._last_file_snapshot = self._read_token_file_snapshot()
                self.logger.info("롤링 토큰 파일 영구 저장 완료: %s", self.token_file)
            except Exception as se:
                self.logger.warning("토큰 파일 영구 저장 실패 (%s): %s", self.token_file, se)

        exp_str = format_kst(self.token_info.expires_at) if self.token_info.expires_at else "None"
        self.logger.info(
            "토큰 갱신 완료: type=%s, token=%s, expires_at=%s",
            self.token_info.token_type, mask_token(self.token_info.access_token), exp_str,
        )
        return self.token_info

    def _refresh_from_env_or_extend(self, reason: str = "manual") -> TokenInfo:
        """PAT 갱신 시 .env/환경 변수에서 변경된 토큰을 확인하고, 동일 PAT면 만료 연장 대신 실패한다."""
        cur_file_env = extract_creds(parse_env_file(self.env_path))
        cur_os_env = extract_creds(os.environ)
        env_token: str | None = None
        refresh_token: str | None = None
        if cur_file_env != self._last_env_file_creds and cur_file_env[0]:
            env_token, refresh_token = cur_file_env[0], cur_file_env[1]
        elif cur_os_env != self._last_os_env_creds and cur_os_env[0]:
            env_token, refresh_token = cur_os_env[0], cur_os_env[1]

        active_token = env_token if env_token else self.token_info.access_token
        token_changed = active_token != self.token_info.access_token or (
            refresh_token is not None and refresh_token != self.token_info.refresh_token
        )
        self._last_env_file_creds, self._last_os_env_creds = cur_file_env, cur_os_env

        if not token_changed and reason != "token_expired":
            raise TokenRefreshError(
                "401 인증 실패 후 .env/토큰 파일에서 변경된 새 토큰을 찾지 못했습니다 (동일 PAT 만료 연장 중단)."
            )

        expires_at = None if token_changed else now_kst() + timedelta(hours=1)
        return TokenInfo(
            access_token=active_token,
            token_type="oauth" if refresh_token else self.token_info.token_type,
            refresh_token=refresh_token or self.token_info.refresh_token,
            expires_at=expires_at,
            client_id=self.token_info.client_id,
            client_secret=self.token_info.client_secret,
            token_url=self.token_info.token_url,
        )

    def _refresh_oauth(self, reason: str = "manual") -> TokenInfo:
        """OAuth 리프레시 토큰 엔드포인트를 호출하여 토큰을 갱신한다."""
        if not self.token_info.client_id or not self.token_info.refresh_token:
            raise TokenRefreshError("OAuth 토큰 갱신에 필요한 client_id, refresh_token이 없습니다.")
        if not self.token_info.client_secret:
            cli_refreshed = sync_token_from_cli(self.token_file)
            if not cli_refreshed:
                raise TokenRefreshError("OAuth 토큰 갱신에 필요한 client_secret이 없으며 CLI 동기화도 실패했습니다.")
            same_tok = cli_refreshed.access_token == self.token_info.access_token
            stale = cli_refreshed.expires_at is None or cli_refreshed.is_expired(DEFAULT_OAUTH_REFRESH_BUFFER_SECONDS)
            if cli_refreshed.is_expired(0) or (same_tok and (stale or reason == "401_unauthorized")):
                raise TokenRefreshError("CLI 동기화 후에도 OAuth 토큰이 갱신되지 않았거나 여전히 만료 상태입니다.")
            return cli_refreshed

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
            new_info = TokenInfo(
                access_token=body["access_token"],
                token_type="oauth",
                refresh_token=body.get("refresh_token", self.token_info.refresh_token),
                expires_at=now_kst() + timedelta(seconds=int(body.get("expires_in", 3600))),
                client_id=self.token_info.client_id,
                client_secret=self.token_info.client_secret,
                token_url=self.token_info.token_url,
            )
            if self.token_file:
                save_token_to_file(new_info, self.token_file)
                self._last_file_snapshot = self._read_token_file_snapshot()
            return new_info
