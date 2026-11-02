from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from server.smartthings.auth import resolve_credential

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
DEFAULT_DB_PATH = Path("server/data/smart_things.db")
DEFAULT_SHARED_SECRET = ""


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    db_path: Path = DEFAULT_DB_PATH
    shared_secret: str = DEFAULT_SHARED_SECRET
    ssl_cert_path: Path | None = None
    ssl_key_path: Path | None = None
    firebase_credentials_path: Path | None = None

    @classmethod
    def from_env(cls, env_path: Path | str = ".env") -> Config:
        host = resolve_credential("SMART_THINGS_HOST", env_path=env_path, default=DEFAULT_HOST)
        port_raw = resolve_credential("SMART_THINGS_PORT", env_path=env_path)
        port = int(port_raw) if port_raw and port_raw.isdigit() else DEFAULT_PORT
        db_raw = resolve_credential("SMART_THINGS_DB_PATH", env_path=env_path)
        db_path = Path(db_raw) if db_raw else DEFAULT_DB_PATH
        shared_secret = (
            resolve_credential("SMARTTHINGS_SHARED_SECRET", env_path=env_path)
            or resolve_credential("SMART_THINGS_SHARED_SECRET", env_path=env_path)
            or DEFAULT_SHARED_SECRET
        )
        ssl_cert_raw = (
            resolve_credential("SMART_THINGS_SSL_CERT_PATH", env_path=env_path)
            or resolve_credential("SMARTTHINGS_SSL_CERT_PATH", env_path=env_path)
        )
        ssl_cert_path = Path(ssl_cert_raw) if ssl_cert_raw else None
        ssl_key_raw = (
            resolve_credential("SMART_THINGS_SSL_KEY_PATH", env_path=env_path)
            or resolve_credential("SMARTTHINGS_SSL_KEY_PATH", env_path=env_path)
        )
        ssl_key_path = Path(ssl_key_raw) if ssl_key_raw else None
        firebase_cred_raw = (
            resolve_credential("FIREBASE_CREDENTIALS_PATH", env_path=env_path)
            or resolve_credential("SMART_THINGS_FIREBASE_CREDENTIALS_PATH", env_path=env_path)
        )
        firebase_credentials_path = Path(firebase_cred_raw) if firebase_cred_raw else None
        return cls(
            host=host,
            port=port,
            db_path=db_path,
            shared_secret=shared_secret,
            ssl_cert_path=ssl_cert_path,
            ssl_key_path=ssl_key_path,
            firebase_credentials_path=firebase_credentials_path,
        )
