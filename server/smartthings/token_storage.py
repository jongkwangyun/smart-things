from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from server.logging_utils import DEFAULT_LOG_BACKUP_COUNT, DEFAULT_LOG_MAX_BYTES, get_rotating_kst_logger
from server.time_utils import KST, format_kst, now_kst, parse_kst

TokenType = Literal["pat", "oauth"]
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TOKEN_FILE = Path("server/data/st_token.json")
logger = logging.getLogger("server.smartthings.token_storage")
CredTuple = tuple[str, str | None, str | None, str | None]


def get_smartthings_logger(
    log_file: Path | str = "server/logs/smartthings.log",
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> logging.Logger:
    """스마트싱스 전용 회전 파일 로거를 설정하고 반환한다."""
    return get_rotating_kst_logger(
        "server.smartthings",
        log_file=log_file,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def parse_env_file(env_path: Path | str = ".env") -> dict[str, str]:
    """지정된 .env 파일에서 환경 변수를 파싱한다."""
    p = Path(env_path)
    if not p.is_file():
        return {}
    results: dict[str, str] = {}
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
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
    return parse_env_file(env_path).get(key, default).strip()


def extract_creds(mapping: dict[str, str] | os._Environ[str]) -> CredTuple:
    """매핑에서 스마트싱스 인증 자격 증명 튜플을 추출한다."""
    token = (
        mapping.get("SMARTTHINGS_TOKEN", "").strip()
        or mapping.get("SMARTTHINGS_PAT", "").strip()
        or mapping.get("SMARTTHINGS_ACCESS_TOKEN", "").strip()
    )
    refresh = mapping.get("SMARTTHINGS_REFRESH_TOKEN", "").strip() or None
    cid = mapping.get("SMARTTHINGS_CLIENT_ID", "").strip() or None
    csec = mapping.get("SMARTTHINGS_CLIENT_SECRET", "").strip() or None
    return (token, refresh, cid, csec)


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


def get_cli_credential_paths() -> list[Path]:
    """Windows 및 Linux(XDG) 환경에서 SmartThings CLI credentials.json 후보 경로 목록을 반환한다."""
    win_paths: list[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        win_paths.extend([
            Path(local_appdata) / "@smartthings/cli/Data/credentials.json",
            Path(local_appdata) / "@smartthings/cli/credentials.json",
        ])
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        win_paths.extend([
            Path(appdata) / "@smartthings/cli/Config/credentials.json",
            Path(appdata) / "@smartthings/cli/credentials.json",
        ])

    xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
    config_root = Path(xdg_config) if xdg_config else Path.home() / ".config"
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    data_root = Path(xdg_data) if xdg_data else Path.home() / ".local/share"

    unix_paths = [
        config_root / "@smartthings/cli/credentials.json",
        data_root / "@smartthings/cli/credentials.json",
        data_root / "@smartthings/cli/Data/credentials.json",
    ]

    ordered = win_paths + unix_paths
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in ordered:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def find_cli_credentials_file() -> Path | None:
    """실제 존재하는 SmartThings CLI credentials.json 파일 경로를 반환한다."""
    for candidate in get_cli_credential_paths():
        if candidate.is_file():
            return candidate
    return None


def find_smartthings_cli_executable() -> Path | None:
    """Windows(smartthings.exe) 및 Linux(smartthings, PATH 포함)에서 SmartThings CLI 실행 파일을 찾는다."""
    if sys.platform == "win32":
        local_candidates = [
            PROJECT_ROOT / "smartthings.exe",
            Path.cwd() / "smartthings.exe",
            PROJECT_ROOT / "smartthings",
            Path.cwd() / "smartthings",
        ]
        which_cmds = ("smartthings.exe", "smartthings")
    else:
        local_candidates = [
            PROJECT_ROOT / "smartthings",
            Path.cwd() / "smartthings",
        ]
        which_cmds = ("smartthings",)

    seen: set[str] = set()
    for candidate in local_candidates:
        key = str(candidate.resolve())
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            return candidate

    for cmd in which_cmds:
        resolved = shutil.which(cmd)
        if resolved:
            path = Path(resolved)
            if path.is_file():
                return path
    return None


def parse_cli_credentials(raw_json: str) -> TokenInfo | None:
    """SmartThings CLI credentials.json 또는 자격 증명 JSON 문자열을 TokenInfo로 파싱한다."""
    try:
        data = json.loads(raw_json)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    entry: dict[str, Any] | None = None
    for candidate_key in ("default:api.smartthings.com", "token"):
        val = data.get(candidate_key)
        if isinstance(val, dict) and (val.get("accessToken") or val.get("access_token")):
            entry = val
            break

    if entry is None:
        if data.get("accessToken") or data.get("access_token"):
            entry = data
        else:
            for val in data.values():
                if isinstance(val, dict) and (val.get("accessToken") or val.get("access_token")):
                    entry = val
                    break

    if entry is None:
        return None

    access_token = str(entry.get("accessToken") or entry.get("access_token") or "").strip()
    if not access_token:
        return None

    raw_refresh = entry.get("refreshToken") or entry.get("refresh_token")
    refresh_token = str(raw_refresh).strip() if raw_refresh else None

    expires_str = entry.get("expires") or entry.get("expires_at") or entry.get("expiresAt")
    expires_at: datetime | None = None
    if isinstance(expires_str, str) and expires_str.strip():
        try:
            expires_at = parse_kst(expires_str)
        except Exception:
            expires_at = None

    raw_client_id = (
        entry.get("clientId")
        or entry.get("client_id")
        or data.get("clientId")
        or data.get("client_id")
    )
    client_id = str(raw_client_id).strip() if raw_client_id else "smartthings-cli"

    return TokenInfo(
        access_token=access_token,
        token_type="oauth",
        refresh_token=refresh_token,
        expires_at=expires_at,
        client_id=client_id,
    )


def sync_token_from_cli(token_file: Path | str | None = None) -> TokenInfo | None:
    """SmartThings CLI의 credentials.json에서 최신 토큰을 읽어와 영구 저장소에 동기화한다."""
    cred_file = find_cli_credentials_file()
    if cred_file is None:
        return None

    target_file = Path(token_file) if token_file else DEFAULT_TOKEN_FILE
    cli_failed = False

    # SmartThings CLI 실행 파일이 있으면 실행하여 최신 토큰으로 갱신 유도
    cli_exe = find_smartthings_cli_executable()
    if cli_exe is not None:
        try:
            env = dict(os.environ)
            for key in ("SMARTTHINGS_TOKEN", "SMARTTHINGS_PAT", "SMARTTHINGS_ACCESS_TOKEN"):
                env.pop(key, None)
            proc = subprocess.run([str(cli_exe), "devices", "-j"], capture_output=True, timeout=15, env=env)
            rc = getattr(proc, "returncode", 0)
            if isinstance(rc, int) and rc != 0:
                cli_failed = True
                logger.warning("SmartThings CLI 실행 실패 (returncode=%s)", rc)
        except Exception as e:
            cli_failed = True
            logger.warning("SmartThings CLI 실행 중 오류: %s", e)
        cred_file = find_cli_credentials_file() or cred_file

    try:
        info = parse_cli_credentials(cred_file.read_text(encoding="utf-8"))
        if info is None:
            return None
        if cli_failed:
            existing = load_token_from_file(target_file)
            fresher_or_same = existing is not None and (
                info.access_token == existing.access_token
                or (existing.expires_at and info.expires_at and info.expires_at <= existing.expires_at)
            )
            if fresher_or_same or info.is_expired(buffer_seconds=0):
                logger.warning("CLI 실행 실패 후 자격 증명이 갱신되지 않아 기존 토큰을 유지합니다.")
                return None
        save_token_to_file(info, target_file)
        return info
    except Exception as e:
        logger.warning("CLI 토큰 동기화 실패: %s", e)
    return None
