from __future__ import annotations

import json
import ssl
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from server.auth import extract_secret_from_headers, is_health_check_path, verify_shared_secret
from server.config import Config
from server.models import AutoStopEvent, WaterTracker
from server.notify import FCMClient, NotificationService, NotificationValidationError
from server.off_periods import OffPeriodConflictError, OffPeriodService, OffPeriodValidationError
from server.reservations import ReservationConflictError, ReservationService, ReservationValidationError
from server.scheduler import ReservationScheduler
from server.storage import Storage
from server.time_utils import format_kst, now_kst
from server.water import WaterService


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

    def _send_item(self, item: Any, not_found_msg: str) -> None:
        if item is None:
            return self._send_error_json(HTTPStatus.NOT_FOUND, not_found_msg)
        self._send_json(HTTPStatus.OK, item.to_dict())

    def _send_delete(self, success: bool, payload: dict[str, Any], not_found_msg: str) -> None:
        if not success:
            return self._send_error_json(HTTPStatus.NOT_FOUND, not_found_msg)
        self._send_json(HTTPStatus.OK, payload)

    def _authenticate(self, path: str) -> bool:
        if is_health_check_path(path) or verify_shared_secret(
            extract_secret_from_headers(self.headers), self.server.config.shared_secret
        ):
            return True
        self._send_error_json(
            HTTPStatus.UNAUTHORIZED, "Unauthorized: missing or invalid shared secret",
            headers={"WWW-Authenticate": 'Bearer realm="smart-things"'},
        )
        return False

    def _read_json(self) -> dict[str, Any] | None:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            return None
        try:
            data = json.loads(self.rfile.read(int(length_header)).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except (ValueError, UnicodeDecodeError):
            return None

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _clean_path(self) -> str:
        return urlparse(self.path).path.rstrip("/") or "/"

    def do_GET(self) -> None:
        path = self._clean_path()
        if is_health_check_path(path):
            return self._send_json(
                HTTPStatus.OK,
                {"status": "ok", "service": "smart-things-mini-pc",
                 "time_kst": format_kst(now_kst()), "database": "connected"},
            )
        if not self._authenticate(path):
            return
        storage = self.server.storage
        if path == "/api/reservations":
            return self._send_json(HTTPStatus.OK, {"reservations": [r.to_dict() for r in storage.list_reservations()]})
        if path.startswith("/api/reservations/"):
            res_id = path.removeprefix("/api/reservations/")
            return self._send_item(storage.get_reservation(res_id), f"Reservation {res_id} not found")
        if path == "/api/off-periods":
            return self._send_json(HTTPStatus.OK, {"off_periods": [o.to_dict() for o in storage.list_off_periods()]})
        if path.startswith("/api/off-periods/"):
            op_id = path.removeprefix("/api/off-periods/")
            return self._send_item(storage.get_off_period(op_id), f"OffPeriod {op_id} not found")
        if path == "/api/auto-stop-events":
            return self._send_json(
                HTTPStatus.OK, {"auto_stop_events": [e.to_dict() for e in storage.list_auto_stop_events()]}
            )
        if path == "/api/water-tracker":
            return self._send_json(HTTPStatus.OK, storage.get_water_tracker().to_dict())
        if path == "/api/notification-targets":
            return self._send_json(
                HTTPStatus.OK, {"notification_targets": [nt.to_dict() for nt in storage.list_notification_targets()]}
            )
        self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")

    def do_POST(self) -> None:
        path = self._clean_path()
        if not self._authenticate(path):
            return

        storage = self.server.storage
        try:
            if path == "/api/water-tracker/refill":
                return self._send_json(HTTPStatus.OK, WaterService(storage).refill_water().to_dict())
            if path.startswith("/api/reservations/") and path.endswith("/cancel"):
                res_id = path.removeprefix("/api/reservations/").removesuffix("/cancel")
                return self._send_item(
                    ReservationService(storage).cancel_reservation(res_id), f"Reservation {res_id} not found"
                )

            data = self._read_json()
            if data is None and path not in ("/api/water-tracker", "/api/notify/test"):
                return self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")

            if path == "/api/reservations":
                assert data is not None
                res = ReservationService(storage).create_reservation(data)
                return self._send_json(HTTPStatus.CREATED, res.to_dict())
            if path == "/api/off-periods":
                assert data is not None
                op = OffPeriodService(storage).create_off_period(data)
                return self._send_json(HTTPStatus.CREATED, op.to_dict())
            if path == "/api/auto-stop-events":
                assert data is not None
                evt = storage.save_auto_stop_event(AutoStopEvent.from_dict(data))
                return self._send_json(HTTPStatus.CREATED, evt.to_dict())
            if path == "/api/water-tracker":
                payload = data or {}
                if "alert_threshold_n" in payload:
                    try:
                        n_val = int(payload["alert_threshold_n"])
                        if n_val < 1:
                            return self._send_error_json(
                                HTTPStatus.BAD_REQUEST, "alert_threshold_n must be an integer >= 1"
                            )
                    except (ValueError, TypeError):
                        return self._send_error_json(
                            HTTPStatus.BAD_REQUEST, "alert_threshold_n must be a valid integer"
                        )
                    if "mop_clean_count" not in payload:
                        return self._send_json(HTTPStatus.OK, WaterService(storage).set_threshold(n_val).to_dict())
                saved = storage.save_water_tracker(WaterTracker.from_dict(payload))
                return self._send_json(HTTPStatus.OK, saved.to_dict())
            if path == "/api/notification-targets":
                assert data is not None
                nt = self.server.notification_service.register_device(
                    str(data.get("device_id", "")).strip(),
                    str(data.get("push_token", "")).strip(),
                    target_id=str(data.get("id", "")).strip() or None,
                )
                return self._send_json(HTTPStatus.CREATED, nt.to_dict())
            if path == "/api/notify/test":
                payload = data or {}
                nt_res = self.server.notification_service.send_test_notification(
                    str(payload.get("title") or "청소기 시험 알림"), str(payload.get("body") or "시험 푸시 알림입니다.")
                )
                return self._send_json(HTTPStatus.OK, nt_res.to_dict())
            self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")
        except (ReservationConflictError, OffPeriodConflictError) as err:
            self._send_error_json(HTTPStatus.CONFLICT, str(err))
        except (
            ReservationValidationError, OffPeriodValidationError, NotificationValidationError, KeyError, ValueError
        ) as err:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(err))

    def do_PUT(self) -> None:
        path = self._clean_path()
        if not self._authenticate(path):
            return
        data = self._read_json()
        if data is None:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid JSON payload")
        try:
            if path.startswith("/api/reservations/"):
                res_id = path.removeprefix("/api/reservations/")
                return self._send_item(
                    ReservationService(self.server.storage).update_reservation(res_id, data),
                    f"Reservation {res_id} not found",
                )
            self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")
        except (ReservationValidationError, KeyError, ValueError) as err:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(err))

    def do_DELETE(self) -> None:
        path = self._clean_path()
        if not self._authenticate(path):
            return
        storage = self.server.storage
        if path.startswith("/api/reservations/"):
            rid = path.removeprefix("/api/reservations/")
            return self._send_delete(
                storage.delete_reservation(rid), {"deleted": True, "id": rid}, f"Reservation {rid} not found"
            )
        if path.startswith("/api/off-periods/"):
            oid = path.removeprefix("/api/off-periods/")
            return self._send_delete(
                storage.delete_off_period(oid), {"deleted": True, "id": oid}, f"OffPeriod {oid} not found"
            )
        if path.startswith("/api/notification-targets/"):
            dev_id = path.removeprefix("/api/notification-targets/")
            return self._send_delete(
                storage.delete_notification_target(dev_id),
                {"deleted": True, "device_id": dev_id},
                f"Device {dev_id} not found",
            )
        self._send_error_json(HTTPStatus.NOT_FOUND, f"Path {path} not found")


class MiniPCServer(ThreadingHTTPServer):
    """스마트싱스 미니PC 서버."""

    def __init__(
        self,
        config: Config,
        storage: Storage | None = None,
        notification_service: NotificationService | None = None,
        scheduler: ReservationScheduler | None = None,
    ) -> None:
        self.config = config
        self.storage = storage or Storage(config.db_path)
        self.notification_service = notification_service or NotificationService(
            self.storage, FCMClient.from_config(config)
        )
        self.scheduler: ReservationScheduler | None = scheduler
        if self.scheduler is not None and getattr(self.scheduler, "notification_service", None) is None:
            self.scheduler.notification_service = self.notification_service
        self.ssl_context: ssl.SSLContext | None = None
        if bool(config.ssl_cert_path) != bool(config.ssl_key_path):
            raise ValueError("Both ssl_cert_path and ssl_key_path must be provided for TLS mode")
        super().__init__((config.host, config.port), MiniPCHandler)
        if config.ssl_cert_path and config.ssl_key_path:
            try:
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
        if self.scheduler is not None and self.scheduler.is_running:
            self.scheduler.stop()
        self.server_close()
        self.storage.close()


def create_server(
    config: Config | None = None,
    db_path: Path | str | None = None,
    shared_secret: str | None = None,
    ssl_cert_path: Path | str | None = None,
    ssl_key_path: Path | str | None = None,
    notification_service: NotificationService | None = None,
    scheduler: ReservationScheduler | None = None,
) -> MiniPCServer:
    cfg = config or Config.from_env()
    cfg.db_path = Path(db_path) if db_path is not None else cfg.db_path
    cfg.shared_secret = shared_secret if shared_secret is not None else cfg.shared_secret
    cfg.ssl_cert_path = Path(ssl_cert_path) if ssl_cert_path is not None else cfg.ssl_cert_path
    cfg.ssl_key_path = Path(ssl_key_path) if ssl_key_path is not None else cfg.ssl_key_path
    return MiniPCServer(cfg, notification_service=notification_service, scheduler=scheduler)


def run_server(config: Config | None = None, start_scheduler: bool = True) -> None:
    srv = create_server(config)
    if start_scheduler:
        if srv.scheduler is None:
            srv.scheduler = ReservationScheduler(storage=srv.storage, notification_service=srv.notification_service)
        srv.scheduler.start()
    scheme = "https" if srv.is_tls else "http"
    print(f"미니PC 서버 시작: {scheme}://{srv.config.host}:{srv.server_address[1]} (DB: {srv.config.db_path})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()
        print("미니PC 서버 종료 완료")
