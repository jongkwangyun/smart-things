"""HTTPS 및 외부 접속(LB-008, REQ-023, D-07, A-06) 단위 및 통합 테스트."""

from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from server.config import Config
from server.deploy.check_external_access import (
    check_external_access,
    check_tls_connection,
    extract_certificate_info,
    parse_x509_tuple,
)
from server.server import create_server


def _generate_test_cert(dest_dir: Path) -> tuple[Path, Path] | None:
    """테스트용 임시 자체서명 인증서와 개인키를 생성한다."""
    openssl = shutil.which("openssl") or next(
        (
            p
            for p in (
                r"C:\Program Files\Git\mingw64\bin\openssl.exe",
                r"C:\Program Files\Git\usr\bin\openssl.exe",
            )
            if os.path.isfile(p)
        ),
        None,
    )
    if not openssl:
        return None
    cert, key = dest_dir / "test_cert.pem", dest_dir / "test_key.pem"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-nodes",
            "-subj",
            "/CN=127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class MockHTTPResponse:
    """테스트용 urllib 응답 컨텍스트 매니저."""

    def __init__(self, code: int, body: bytes) -> None:
        self.code = code
        self.body = body

    def getcode(self) -> int:
        return self.code

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> MockHTTPResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


class TestHTTPSConfigAndServer(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.temp_path = Path(self.temp_dir.name)
        self.db_path = self.temp_path / "test.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_config_tls_fields_and_env_loading(self) -> None:
        """Config 기본값 및 환경변수를 통한 SSL 경로 주입을 검증한다."""
        cfg_default = Config()
        self.assertIsNone(cfg_default.ssl_cert_path)
        self.assertIsNone(cfg_default.ssl_key_path)

        cert_file = self.temp_path / "server.crt"
        key_file = self.temp_path / "server.key"
        with patch.dict(
            os.environ,
            {"SMART_THINGS_SSL_CERT_PATH": str(cert_file), "SMART_THINGS_SSL_KEY_PATH": str(key_file)},
        ):
            cfg_env = Config.from_env()
            self.assertEqual(cfg_env.ssl_cert_path, cert_file)
            self.assertEqual(cfg_env.ssl_key_path, key_file)

    def test_server_partial_tls_configuration_raises_error(self) -> None:
        """인증서 경로와 개인키 경로 중 하나만 제공되면 ValueError가 발생해야 한다."""
        cert_file, key_file = self.temp_path / "server.crt", self.temp_path / "server.key"
        with self.assertRaises(ValueError):
            create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, ssl_cert_path=cert_file))
        with self.assertRaises(ValueError):
            create_server(Config(host="127.0.0.1", port=0, db_path=self.db_path, ssl_key_path=key_file))

    def test_server_non_existent_tls_files_raise_error(self) -> None:
        """존재하지 않는 인증서/개인키 파일 경로 지정 시 FileNotFoundError가 발생해야 한다."""
        cfg = Config(
            host="127.0.0.1",
            port=0,
            db_path=self.db_path,
            ssl_cert_path=self.temp_path / "not_exist.crt",
            ssl_key_path=self.temp_path / "not_exist.key",
        )
        with self.assertRaises(FileNotFoundError):
            create_server(cfg)

    def test_server_tls_initialization_with_generated_cert(self) -> None:
        """유효한 테스트 인증서와 개인키로 MiniPCServer가 TLS 모드로 정상 초기화되는지 검증한다."""
        pair = _generate_test_cert(self.temp_path)
        if not pair:
            self.skipTest("OpenSSL 바이너리를 찾을 수 없어 테스트 인증서 생성을 건너뜁니다.")
        cert_file, key_file = pair

        cfg = Config(
            host="127.0.0.1",
            port=0,
            db_path=self.db_path,
            ssl_cert_path=cert_file,
            ssl_key_path=key_file,
        )
        srv = create_server(cfg)
        try:
            self.assertTrue(srv.is_tls)
            self.assertIsNotNone(srv.ssl_context)
            self.assertIsInstance(srv.ssl_context, ssl.SSLContext)
        finally:
            srv.close()

    def test_in_memory_tls_handshake_and_request_exchange(self) -> None:
        """MemoryBIO를 통해 실제 암호화 TLS 핸드셰이크 및 요청-응답 교환을 검증한다."""
        pair = _generate_test_cert(self.temp_path)
        if not pair:
            self.skipTest("OpenSSL 바이너리를 찾을 수 없습니다.")
        cert_file, key_file = pair

        cfg = Config(
            host="127.0.0.1",
            port=0,
            db_path=self.db_path,
            ssl_cert_path=cert_file,
            ssl_key_path=key_file,
        )
        srv = create_server(cfg)
        try:
            client_ctx = ssl.create_default_context()
            client_ctx.check_hostname = False
            client_ctx.verify_mode = ssl.CERT_NONE

            c_in, c_out = ssl.MemoryBIO(), ssl.MemoryBIO()
            s_in, s_out = ssl.MemoryBIO(), ssl.MemoryBIO()
            c_ssl = client_ctx.wrap_bio(c_in, c_out, server_side=False)
            assert srv.ssl_context is not None
            s_ssl = srv.ssl_context.wrap_bio(s_in, s_out, server_side=True)

            for _ in range(8):
                try:
                    c_ssl.do_handshake()
                except ssl.SSLWantReadError:
                    pass
                s_in.write(c_out.read())
                try:
                    s_ssl.do_handshake()
                except ssl.SSLWantReadError:
                    pass
                c_in.write(s_out.read())

            self.assertIn("TLS", c_ssl.version() or "")
            self.assertIsNotNone(c_ssl.cipher())

            req_data = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
            c_ssl.write(req_data)
            s_in.write(c_out.read())
            self.assertEqual(s_ssl.read(len(req_data)), req_data)
        finally:
            srv.close()


class TestCheckExternalAccessTool(unittest.TestCase):
    def test_parse_x509_tuple(self) -> None:
        rdn = ((("commonName", "myhome.iptime.org"),), (("organizationName", "Let's Encrypt"),))
        parsed = parse_x509_tuple(rdn)
        self.assertEqual(parsed["commonName"], "myhome.iptime.org")
        self.assertEqual(parsed["organizationName"], "Let's Encrypt")

    def test_extract_certificate_info_letsencrypt(self) -> None:
        fake_cert = {
            "subject": ((("commonName", "cleaner.ddns.iptime.org"),),),
            "issuer": ((("organizationName", "Let's Encrypt"), ("commonName", "R3")),),
            "subjectAltName": (("DNS", "cleaner.ddns.iptime.org"),),
            "notBefore": "Oct 01 00:00:00 2026 GMT",
            "notAfter": "Dec 30 23:59:59 2026 GMT",
        }
        info = extract_certificate_info(fake_cert)
        self.assertEqual(info.subject, "cleaner.ddns.iptime.org")
        self.assertEqual(info.issuer, "Let's Encrypt")
        self.assertIn("DNS:cleaner.ddns.iptime.org", info.san)
        self.assertFalse(info.is_self_signed)

    def test_extract_certificate_info_self_signed_warning(self) -> None:
        fake_cert = {
            "subject": ((("commonName", "127.0.0.1"),),),
            "issuer": ((("commonName", "127.0.0.1"),),),
            "notBefore": "Oct 01 00:00:00 2026 GMT",
            "notAfter": "Oct 02 00:00:00 2026 GMT",
        }
        info = extract_certificate_info(fake_cert)
        self.assertTrue(info.is_self_signed)
        self.assertIsNotNone(info.warning)
        self.assertIn("자체 서명", info.warning or "")

    def test_check_tls_connection_mock(self) -> None:
        mock_sock = MagicMock()
        mock_sock.getpeercert.return_value = {
            "subject": ((("commonName", "test.ddns.org"),),),
            "issuer": ((("organizationName", "Let's Encrypt"),),),
            "notAfter": "Dec 30 23:59:59 2026 GMT",
        }
        ok, cert_info, err = check_tls_connection("test.ddns.org", 443, connect_fn=lambda *args: mock_sock)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertIsNotNone(cert_info)
        assert cert_info is not None
        self.assertEqual(cert_info.subject, "test.ddns.org")

    def test_check_external_access_success(self) -> None:
        mock_sock = MagicMock()
        mock_sock.getpeercert.return_value = {
            "subject": ((("commonName", "cleaner.iptime.org"),),),
            "issuer": ((("organizationName", "ZeroSSL"),),),
            "notAfter": "Dec 30 23:59:59 2026 GMT",
        }
        payload = {"status": "ok", "service": "smart-things-mini-pc", "time_kst": "2026-10-30 12:00:00+09:00"}
        body_bytes = json.dumps(payload).encode("utf-8")

        res = check_external_access(
            host="cleaner.iptime.org",
            port=443,
            connect_fn=lambda *args: mock_sock,
            urlopen_fn=lambda *args, **kwargs: MockHTTPResponse(200, body_bytes),
        )
        self.assertTrue(res.is_success)
        self.assertTrue(res.tls_handshake)
        self.assertTrue(res.health_ok)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.service_payload, payload)
        self.assertEqual(res.errors, [])


class TestDeploymentConfigurations(unittest.TestCase):
    def setUp(self) -> None:
        self.deploy_dir = Path(__file__).resolve().parent.parent / "server" / "deploy"

    def test_caddyfile_exists_and_contains_required_directives(self) -> None:
        caddyfile = self.deploy_dir / "Caddyfile"
        self.assertTrue(caddyfile.is_file(), f"Caddyfile 부재: {caddyfile}")
        content = caddyfile.read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:8080", content)
        self.assertIn("reverse_proxy", content)
        self.assertIn("Strict-Transport-Security", content)
        self.assertIn("X-Forwarded-Proto https", content)
        self.assertIn("{$SMART_THINGS_DOMAIN:", content)

    def test_setup_https_powershell_script_exists_and_references_ports(self) -> None:
        script = self.deploy_dir / "setup_https.ps1"
        self.assertTrue(script.is_file(), f"setup_https.ps1 부재: {script}")
        content = script.read_text(encoding="utf-8")
        self.assertIn("443", content)
        self.assertIn("80", content)
        self.assertIn("8080", content)
        self.assertIn("DDNS", content)
        self.assertIn("VPN", content)
