from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

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

    @classmethod
    def from_env(cls) -> Config:
        host = os.environ.get("SMART_THINGS_HOST", DEFAULT_HOST)
        port_raw = os.environ.get("SMART_THINGS_PORT")
        port = int(port_raw) if port_raw and port_raw.isdigit() else DEFAULT_PORT
        db_raw = os.environ.get("SMART_THINGS_DB_PATH")
        db_path = Path(db_raw) if db_raw else DEFAULT_DB_PATH
        shared_secret = (
            os.environ.get("SMARTTHINGS_SHARED_SECRET")
            or os.environ.get("SMART_THINGS_SHARED_SECRET")
            or DEFAULT_SHARED_SECRET
        )
        ssl_cert_raw = (
            os.environ.get("SMART_THINGS_SSL_CERT_PATH")
            or os.environ.get("SMARTTHINGS_SSL_CERT_PATH")
        )
        ssl_cert_path = Path(ssl_cert_raw) if ssl_cert_raw else None
        ssl_key_raw = (
            os.environ.get("SMART_THINGS_SSL_KEY_PATH")
            or os.environ.get("SMARTTHINGS_SSL_KEY_PATH")
        )
        ssl_key_path = Path(ssl_key_raw) if ssl_key_raw else None
        return cls(
            host=host,
            port=port,
            db_path=db_path,
            shared_secret=shared_secret,
            ssl_cert_path=ssl_cert_path,
            ssl_key_path=ssl_key_path,
        )
