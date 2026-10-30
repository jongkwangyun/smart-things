from __future__ import annotations

import json
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from server.auth import (
    extract_secret_from_headers,
    is_health_check_path,
    verify_shared_secret,
)
from server.config import Config
from server.models import AutoStopEvent, NotificationTarget, WaterTracker
from server.off_periods import (
    OffPeriodConflictError,
    OffPeriodService,
    OffPeriodValidationError,
)
from server.reservations import (
    ReservationConflictError,
    ReservationService,
    ReservationValidationError,
)
from server.storage import Storage
from server.time_utils import format_kst, now_kst


class MiniPCHandler(BaseHTTPRequestHandler):
    """미니PC HTTP 요청 핸들러."""

    server: MiniPCServer

    def _send_json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        self._send_json(status, {"error": message, "status": status}, headers=headers)

    def _authenticate(self, path: str) -> bool:
        if is_health_check_path(path):
            return True
        provided = extract_secret_from_headers(self.headers)
        if verify_shared_secret(provided, self.server.config.shared_secret):
            return True
        self._send_error_json(
            HTTPStatus.UNAUTHORIZED,
            "Unauthorized: missing or invalid shared secret",
            headers={"WWW-Authenticate": 'Bearer realm="smart-things"'},
        )
        return False

    def _read_json(self) -> dict[str, Any] | None:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return None
        try:
            length = int(length_header)
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _clean_path(self) -> str:
        path = urlparse(self.path).path.rstrip("/")
        return path if path else "/"

    def do_GET(self) -> None:
        path = self._clean_path()

        # 상태 확인 주소
        if is_health_check_path(path):
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "smart-things-mini-pc",
                    "time_kst": format_kst(now_kst()),
                    "database": "connected",
                },
            )
            return

        if not self._authenticate(path):
            return

        storage = self.server.storage
        if path == "/api/reservations":
            items = [r.to_dict() for r in storage.list_reservations()]
            self._send_json(HTTPStatus.OK, {"reservations": items})
            return

        if path.startswith("/api/reservations/"):
            res_id = path.removeprefix("/api/reservations/")
            res = storage.get_reservation(res_id)
            if not res:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"Reservation {res_id} not found")
            else:
                self._send_json(HTTPStatus.OK, res.to_dict())
            return

        if path == "/api/off-periods":
            items = [op.to_dict() for op in storage.list_off_periods()]
            self._send_json(HTTPStatus.OK, {"off_periods": items})
            return

        if path.startswith("/api/off-periods/"):
            op_id = path.removeprefix("/api/off-periods/")
            op = storage.get_off_period(op_id)
            if not op:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"OffPeriod {op_id} not found")
            else:
                self._send_json(HTTPStatus.OK, op.to_dict())
            return

        if path == "/api/auto-stop-events":
            items = [e.to_dict() for e in storage.list_auto_stop_events()]
            self._send_json(HTTPStatus.OK, {"auto_stop_events": items})
            return

        if path == "/api/water-tracker":
            tracker = storage.get_water_tracker()
            self._send_json(HTTPStatus.OK, tracker.to_dict())
            return

        if path == "/api/notification-targets":
            items = [nt.to_dict() for nt in storage.list_notification_targets()]
            self._send_json(HTTPStatus.OK, {"notification_targets": items})
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")

    def do_POST(self) -> None:
        path = self._clean_path()

        if not self._authenticate(path):
            return

        storage = self.server.storage

        data = self._read_json()
        if data is None and path not in ("/api/water-tracker",):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
            return

        try:
            if path == "/api/reservations":
                assert data is not None
                res = ReservationService(storage).create_reservation(data)
                self._send_json(HTTPStatus.CREATED, res.to_dict())
                return

            if path == "/api/off-periods":
                assert data is not None
                op = OffPeriodService(storage).create_off_period(data)
                self._send_json(HTTPStatus.CREATED, op.to_dict())
                return

            if path == "/api/auto-stop-events":
                assert data is not None
                event = AutoStopEvent.from_dict(data)
                storage.save_auto_stop_event(event)
                self._send_json(HTTPStatus.CREATED, event.to_dict())
                return

            if path == "/api/water-tracker":
                payload = data or {}
                tracker = WaterTracker.from_dict(payload)
                storage.save_water_tracker(tracker)
                self._send_json(HTTPStatus.OK, tracker.to_dict())
                return

            if path == "/api/notification-targets":
                assert data is not None
                nt = NotificationTarget.from_dict(data)
                storage.save_notification_target(nt)
                self._send_json(HTTPStatus.CREATED, nt.to_dict())
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")
        except (ReservationValidationError, OffPeriodValidationError) as err:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(err))
        except (ReservationConflictError, OffPeriodConflictError) as err:
            self._send_error_json(HTTPStatus.CONFLICT, str(err))
        except (KeyError, ValueError) as err:
            self._send_error_json(HTTPStatus.BAD_REQUEST, f"Invalid model payload: {err}")

    def do_DELETE(self) -> None:
        path = self._clean_path()

        if not self._authenticate(path):
            return

        storage = self.server.storage

        if path.startswith("/api/reservations/"):
            res_id = path.removeprefix("/api/reservations/")
            if not storage.delete_reservation(res_id):
                self._send_error_json(HTTPStatus.NOT_FOUND, f"Reservation {res_id} not found")
            else:
                self._send_json(HTTPStatus.OK, {"deleted": True, "id": res_id})
            return

        if path.startswith("/api/off-periods/"):
            op_id = path.removeprefix("/api/off-periods/")
            if not storage.delete_off_period(op_id):
                self._send_error_json(HTTPStatus.NOT_FOUND, f"OffPeriod {op_id} not found")
            else:
                self._send_json(HTTPStatus.OK, {"deleted": True, "id": op_id})
            return

        if path.startswith("/api/notification-targets/"):
            device_id = path.removeprefix("/api/notification-targets/")
            if not storage.delete_notification_target(device_id):
                self._send_error_json(HTTPStatus.NOT_FOUND, f"Device {device_id} not found")
            else:
                self._send_json(HTTPStatus.OK, {"deleted": True, "device_id": device_id})
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")


class MiniPCServer(ThreadingHTTPServer):
    """스마트싱스 미니PC 서버."""

    def __init__(self, config: Config, storage: Storage | None = None) -> None:
        self.config = config
        self.storage = storage or Storage(config.db_path)
        self.ssl_context: ssl.SSLContext | None = None
        super().__init__((config.host, config.port), MiniPCHandler)
        try:
            if bool(config.ssl_cert_path) != bool(config.ssl_key_path):
                raise ValueError("Both ssl_cert_path and ssl_key_path must be provided for TLS mode")
            if config.ssl_cert_path and config.ssl_key_path:
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                ctx.load_cert_chain(certfile=str(config.ssl_cert_path), keyfile=str(config.ssl_key_path))
                self.socket = ctx.wrap_socket(self.socket, server_side=True)
                self.ssl_context = ctx
        except Exception:
            self.close()
            raise

    @property
    def is_tls(self) -> bool:
        return self.ssl_context is not None

    def close(self) -> None:
        self.server_close()
        self.storage.close()


def create_server(
    config: Config | None = None,
    db_path: Path | str | None = None,
    shared_secret: str | None = None,
    ssl_cert_path: Path | str | None = None,
    ssl_key_path: Path | str | None = None,
) -> MiniPCServer:
    cfg = config or Config.from_env()
    if db_path is not None:
        cfg.db_path = Path(db_path)
    if shared_secret is not None:
        cfg.shared_secret = shared_secret
    if ssl_cert_path is not None:
        cfg.ssl_cert_path = Path(ssl_cert_path)
    if ssl_key_path is not None:
        cfg.ssl_key_path = Path(ssl_key_path)
    return MiniPCServer(cfg)


def run_server(config: Config | None = None) -> None:
    srv = create_server(config)
    host, port = str(srv.server_address[0]), int(srv.server_address[1])
    scheme = "https" if srv.is_tls else "http"
    print(f"미니PC 서버 시작: {scheme}://{host}:{port} (DB: {srv.config.db_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        print("미니PC 서버 종료 완료")
