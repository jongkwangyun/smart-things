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
    parser.add_argument("--secret", default=None, help="공유 비밀키 (지정하지 않으면 환경변수 사용)")
    parser.add_argument("--ssl-cert", type=Path, default=None, help="SSL 인증서 (.crt/.pem) 경로")
    parser.add_argument("--ssl-key", type=Path, default=None, help="SSL 개인키 (.key) 경로")
    parser.add_argument("--no-scheduler", action="store_true", help="예약 스케줄러 비활성화")
    args = parser.parse_args()

    cfg = Config.from_env()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.db:
        cfg.db_path = args.db
    if args.secret:
        cfg.shared_secret = args.secret
    if args.ssl_cert:
        cfg.ssl_cert_path = args.ssl_cert
    if args.ssl_key:
        cfg.ssl_key_path = args.ssl_key

    run_server(cfg, start_scheduler=not args.no_scheduler)


if __name__ == "__main__":
    main()
