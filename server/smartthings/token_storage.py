from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from server.time_utils import KST, format_kst, now_kst, parse_kst

TokenType = Literal["pat", "oauth"]
DEFAULT_TOKEN_FILE = Path("server/data/st_token.json")
logger = logging.getLogger("server.smartthings.token_storage")


def mask_token(token: str | None) -> str:
    """보안을 위해 토큰의 앞/뒤 3글자만 남기고 마스킹한다."""
    if not token:
        return "[EMPTY]"
    token_str = token.strip()
    if len(token_str) <= 8:
        return "***"
    return f"{token_str[:3]}***{token_str[-3:]}"


@dataclass
class TokenInfo:
    """스마트싱스 인증 토큰 메타데이터 및 만료 관리."""

    access_token: str
    token_type: TokenType = "pat"
    refresh_token: str | None = None
    expires_at: datetime | None = None
    client_id: str | None = None
    client_secret: str | None = None
    token_url: str = "https://api.smartthings.com/oauth/token"

    def is_expired(self, buffer_seconds: int = 60) -> bool:
        """만료 시각 버퍼를 고려하여 토큰 만료 여부를 판별한다. (선제 감지 지원: buffer_seconds=3600 등)."""
        if self.expires_at is None:
            return False
        cutoff = self.expires_at - timedelta(seconds=buffer_seconds)
        return now_kst() >= cutoff

    def to_dict(self) -> dict[str, Any]:
        """로깅 또는 표시용 딕셔너리 (토큰 마스킹)."""
        return {
            "token_type": self.token_type,
            "access_token": mask_token(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "expires_at": format_kst(self.expires_at) if self.expires_at else None,
        }

    def to_storage_dict(self) -> dict[str, Any]:
        """영구 파일 저장용 원본 딕셔너리."""
        return {
            "token_type": self.token_type,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "token_url": self.token_url,
            "updated_at": now_kst().isoformat(),
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> TokenInfo:
        """저장용 딕셔너리로부터 TokenInfo 객체 생성."""
        expires_at_raw = data.get("expires_at")
        expires_at: datetime | None = None
        if expires_at_raw:
            try:
                dt = datetime.fromisoformat(expires_at_raw)
                if dt.tzinfo is None:
                    expires_at = dt.replace(tzinfo=KST)
                else:
                    expires_at = dt.astimezone(KST)
            except Exception:
                expires_at = None

        raw_type = data.get("token_type", "pat")
        token_type: TokenType = "oauth" if raw_type == "oauth" else "pat"

        return cls(
            access_token=str(data.get("access_token", "")),
            token_type=token_type,
            refresh_token=data.get("refresh_token") or None,
            expires_at=expires_at,
            client_id=data.get("client_id") or None,
            client_secret=data.get("client_secret") or None,
            token_url=str(data.get("token_url", "https://api.smartthings.com/oauth/token")),
        )


def save_token_to_file(token_info: TokenInfo, file_path: Path | str = DEFAULT_TOKEN_FILE) -> None:
    """토큰 정보를 로컬 파일에 원자적으로 저장한다 (tempfile -> replace)."""
    target_path = Path(file_path).resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    data = token_info.to_storage_dict()
    temp_file: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=target_path.parent,
            delete=False,
            encoding="utf-8",
            suffix=".tmp",
        ) as tf:
            temp_file = Path(tf.name)
            json.dump(data, tf, ensure_ascii=False, indent=2)
            tf.flush()
            os.fsync(tf.fileno())

        try:
            temp_file.chmod(0o600)
        except OSError:
            pass

        temp_file.replace(target_path)
    except Exception as e:
        if temp_file and temp_file.exists():
            try:
                temp_file.unlink()
            except OSError:
                pass
        logger.error("토큰 파일 저장 실패 (%s): %s", target_path, e)
        raise OSError(f"토큰 파일 원자적 저장 실패: {e}") from e


def load_token_from_file(file_path: Path | str = DEFAULT_TOKEN_FILE) -> TokenInfo | None:
    """영구 파일에서 토큰 정보를 로드한다. 파일이 없거나 손상된 경우 None을 반환한다."""
    path = Path(file_path).resolve()
    if not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not isinstance(data, dict):
            return None
        access_token = data.get("access_token")
        if not access_token or not str(access_token).strip():
            return None
        return TokenInfo.from_storage_dict(data)
    except Exception as e:
        logger.warning("토큰 파일 로드 실패 (%s): %s", path, e)
        return None


def sync_token_from_cli(token_file: Path | str | None = None) -> TokenInfo | None:
    """SmartThings CLI의 credentials.json에서 최신 토큰을 읽어와 영구 저장소에 동기화한다."""
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return None
    cred_file = Path(local_appdata) / "@smartthings/cli/Data/credentials.json"
    if not cred_file.is_file():
        return None

    target_file = Path(token_file) if token_file else DEFAULT_TOKEN_FILE

    # smartthings.exe가 있으면 실행하여 최신 토큰으로 갱신 유도
    cli_exe = Path("smartthings.exe").resolve()
    if cli_exe.is_file():
        try:
            import subprocess

            env = dict(os.environ)
            env.pop("SMARTTHINGS_TOKEN", None)
            env.pop("SMARTTHINGS_PAT", None)
            subprocess.run([str(cli_exe), "devices", "-j"], capture_output=True, timeout=15, env=env)
        except Exception:
            pass

    try:
        content = cred_file.read_text(encoding="utf-8")
        data = json.loads(content)
        entry = data.get("default:api.smartthings.com")
        if not entry and isinstance(data, dict) and data:
            entry = next(iter(data.values()))
        if not isinstance(entry, dict):
            return None

        access_token = entry.get("accessToken") or entry.get("access_token")
        refresh_token = entry.get("refreshToken") or entry.get("refresh_token")
        expires_str = entry.get("expires")
        expires_at = parse_kst(expires_str) if expires_str else None

        if access_token:
            info = TokenInfo(
                access_token=str(access_token),
                token_type="oauth",
                refresh_token=str(refresh_token) if refresh_token else None,
                expires_at=expires_at,
                client_id="smartthings-cli",
            )
            save_token_to_file(info, target_file)
            return info
    except Exception as e:
        logger.warning("CLI 토큰 동기화 실패: %s", e)
    return None
