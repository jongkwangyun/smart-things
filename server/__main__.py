from __future__ import annotations

import argparse
from pathlib import Path

from server.config import Config
from server.server import run_server


def main() -> None:
    parser = argparse.ArgumentParser(description="스마트싱스 미니PC 서버")
    parser.add_argument("--host", default=None, help="바인딩 호스트 (기본: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="바인딩 포트 (기본: 8080)")
    parser.add_argument("--db", type=Path, default=None, help="SQLite DB 파일 경로")
    args = parser.parse_args()

    cfg = Config.from_env()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.db:
        cfg.db_path = args.db

    run_server(cfg)


if __name__ == "__main__":
    main()
