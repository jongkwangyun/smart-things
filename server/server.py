from __future__ import annotations

import json
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
from server.models import (
    AutoStopEvent,
    NotificationTarget,
    OffPeriod,
    WaterTracker,
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
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        self._send_json(status, {"error": message, "status": status}, headers=headers)

    def _authenticate(self, path: str) -> bool:
        if is_health_check_path(path):
            return True
        provided = extract_secret_from_headers(self.headers)
        expected = self.server.config.shared_secret
        if verify_shared_secret(provided, expected):
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
        # 조용한 테스트/실행을 위해 기본 로깅 생략 (필요시 커스텀 로거 연결)
        pass

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

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
            res_id = path.split("/api/reservations/")[1]
            res = storage.get_reservation(res_id)
            if not res:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"Reservation {res_id} not found")
                return
            self._send_json(HTTPStatus.OK, res.to_dict())
            return

        if path == "/api/off-periods":
            items = [op.to_dict() for op in storage.list_off_periods()]
            self._send_json(HTTPStatus.OK, {"off_periods": items})
            return

        if path.startswith("/api/off-periods/"):
            op_id = path.split("/api/off-periods/")[1]
            op = storage.get_off_period(op_id)
            if not op:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"OffPeriod {op_id} not found")
                return
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
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

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
                op = OffPeriod.from_dict(data)
                storage.save_off_period(op)
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
        except ReservationValidationError as err:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(err))
            return
        except ReservationConflictError as err:
            self._send_error_json(HTTPStatus.CONFLICT, str(err))
            return
        except (KeyError, ValueError) as err:
            self._send_error_json(HTTPStatus.BAD_REQUEST, f"Invalid model payload: {err}")
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if not path:
            path = "/"

        if not self._authenticate(path):
            return

        storage = self.server.storage

        if path.startswith("/api/reservations/"):
            res_id = path.split("/api/reservations/")[1]
            deleted = storage.delete_reservation(res_id)
            if not deleted:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"Reservation {res_id} not found")
                return
            self._send_json(HTTPStatus.OK, {"deleted": True, "id": res_id})
            return

        if path.startswith("/api/off-periods/"):
            op_id = path.split("/api/off-periods/")[1]
            deleted = storage.delete_off_period(op_id)
            if not deleted:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"OffPeriod {op_id} not found")
                return
            self._send_json(HTTPStatus.OK, {"deleted": True, "id": op_id})
            return

        if path.startswith("/api/notification-targets/"):
            device_id = path.split("/api/notification-targets/")[1]
            deleted = storage.delete_notification_target(device_id)
            if not deleted:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"Device {device_id} not found")
                return
            self._send_json(HTTPStatus.OK, {"deleted": True, "device_id": device_id})
            return

        self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")


class MiniPCServer(ThreadingHTTPServer):
    """스마트싱스 미니PC 서버."""

    def __init__(self, config: Config, storage: Storage | None = None) -> None:
        self.config = config
        self.storage = storage or Storage(config.db_path)
        super().__init__((config.host, config.port), MiniPCHandler)

    def close(self) -> None:
        self.server_close()
        self.storage.close()


def create_server(
    config: Config | None = None,
    db_path: Path | str | None = None,
    shared_secret: str | None = None,
) -> MiniPCServer:
    cfg = config or Config.from_env()
    if db_path is not None:
        cfg.db_path = Path(db_path)
    if shared_secret is not None:
        cfg.shared_secret = shared_secret
    return MiniPCServer(cfg)


def run_server(config: Config | None = None) -> None:
    srv = create_server(config)
    addr = srv.server_address
    host_str = str(addr[0])
    port_int = int(addr[1])
    print(f"미니PC 서버 시작: http://{host_str}:{port_int} (DB: {srv.config.db_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        print("미니PC 서버 종료 완료")
