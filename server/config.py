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
        return cls(host=host, port=port, db_path=db_path, shared_secret=shared_secret)
