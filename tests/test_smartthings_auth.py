from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from server.smartthings.auth import (
    SmartThingsTokenManager,
    TokenInfo,
    TokenRefreshError,
    mask_token,
    parse_env_file,
    resolve_credential,
)
from server.smartthings.client import (
    DeviceStatus,
    SmartThingsApiError,
    SmartThingsAuthError,
    SmartThingsClient,
)
from server.smartthings.verify import verify_smartthings_connection
from server.time_utils import now_kst


class TestSmartThingsAuth(unittest.TestCase):
    """스마트싱스 인증 연결, 만료 감지, 자동 갱신 및 로깅 테스트 (LB-005, LB-001)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.log_path = Path(self.temp_dir.name) / "smartthings_test.log"
        self.env_path = Path(self.temp_dir.name) / ".env.test"
        self.device_id = "03431570-5acf-f89e-7b39-275ce15d3258"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_mask_token(self) -> None:
        """토큰 마스킹 함수가 민감한 정보를 노출하지 않는지 검증."""
        self.assertEqual(mask_token(""), "[EMPTY]")
        self.assertEqual(mask_token(None), "[EMPTY]")
        self.assertEqual(mask_token("12345678"), "***")
        masked = mask_token("secret_smartthings_token_value_99")
        self.assertTrue(masked.startswith("sec***"))
        self.assertTrue(masked.endswith("***_99"))
        self.assertNotIn("smartthings_token_value", masked)

    def test_token_info_expiration_detection(self) -> None:
        """TokenInfo의 만료 감지 및 버퍼 시간 로직 검증."""
        self.assertFalse(TokenInfo(access_token="test-token", token_type="pat").is_expired())
        self.assertFalse(TokenInfo(access_token="test-token", expires_at=now_kst() + timedelta(hours=1)).is_expired())
        self.assertTrue(TokenInfo(access_token="test-token", expires_at=now_kst() - timedelta(seconds=10)).is_expired())
        info_buffer = TokenInfo(access_token="test-token", expires_at=now_kst() + timedelta(seconds=30))
        self.assertTrue(info_buffer.is_expired(buffer_seconds=60))
        self.assertFalse(info_buffer.is_expired(buffer_seconds=10))

    def test_token_manager_force_expire_and_auto_refresh(self) -> None:
        """토큰 강제 만료 후 get_token 호출 시 자동 갱신 검증."""
        refresh_called = False

        def mock_refresher(info: TokenInfo) -> TokenInfo:
            nonlocal refresh_called
            refresh_called = True
            return TokenInfo(
                access_token="refreshed-xyz", token_type=info.token_type, expires_at=now_kst() + timedelta(hours=2)
            )

        token_info = TokenInfo(access_token="init-abc", token_type="pat", expires_at=now_kst() + timedelta(hours=1))
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path, refresh_handler=mock_refresher)
        self.assertEqual(manager.get_token(), "init-abc")
        self.assertFalse(refresh_called)
        manager.force_expire()
        self.assertTrue(manager.is_expired())
        token_after = manager.get_token()
        self.assertTrue(refresh_called)
        self.assertEqual(token_after, "refreshed-xyz")
        self.assertFalse(manager.is_expired())

    def test_token_manager_logging_and_masking(self) -> None:
        """토큰 갱신 로그가 KST 일시와 마스킹된 토큰으로 파일에 기록되는지 검증."""
        raw_secret = "ultra_sensitive_secret_token_12345"
        token_info = TokenInfo(access_token=raw_secret, token_type="pat")
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path)
        manager.force_expire()
        _ = manager.get_token()
        self.assertTrue(self.log_path.is_file())
        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("스마트싱스 토큰 강제 만료 설정 완료", log_text)
        self.assertIn("토큰 만료 감지됨", log_text)
        self.assertIn("토큰 갱신 완료", log_text)
        self.assertNotIn(raw_secret, log_text)
        self.assertIn(mask_token(raw_secret), log_text)

    def test_env_file_parsing_and_credential_resolution(self) -> None:
        """환경 변수 및 .env 파일 자격 증명 탐색 검증."""
        self.env_path.write_text("SMARTTHINGS_TOKEN=env_tok_88\nSMARTTHINGS_DEVICE_ID=test-dev-123\n", encoding="utf-8")
        parsed = parse_env_file(self.env_path)
        self.assertEqual(parsed["SMARTTHINGS_TOKEN"], "env_tok_88")
        self.assertEqual(parsed["SMARTTHINGS_DEVICE_ID"], "test-dev-123")
        self.assertEqual(resolve_credential("SMARTTHINGS_TOKEN", env_path=self.env_path), "env_tok_88")
        with patch.dict("os.environ", {"SMARTTHINGS_TOKEN": "os_env_tok_99"}):
            self.assertEqual(resolve_credential("SMARTTHINGS_TOKEN", env_path=self.env_path), "os_env_tok_99")

    def test_oauth_refresh_workflow(self) -> None:
        """OAuth 토큰 갱신 워크플로우 검증."""
        token_info = TokenInfo(
            access_token="old-tok", token_type="oauth", refresh_token="ref-tok", client_id="cid", client_secret="csec",
            token_url="https://api.smartthings.com/oauth/token", expires_at=now_kst() - timedelta(minutes=5),
        )
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path)
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "access_token": "new-tok", "refresh_token": "new-ref", "expires_in": 7200,
        }).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp):
            self.assertEqual(manager.get_token(), "new-tok")
            self.assertEqual(manager.token_info.refresh_token, "new-ref")
            self.assertFalse(manager.is_expired())
        invalid_oauth = TokenInfo(access_token="tok", token_type="oauth", refresh_token="ref")
        invalid_mgr = SmartThingsTokenManager(token_info=invalid_oauth, log_file=self.log_path)
        with self.assertRaises(TokenRefreshError):
            invalid_mgr.refresh()

    def test_client_get_device_status_parsing(self) -> None:
        """SmartThingsClient 기기 상태 JSON 파싱 검증."""
        token_info = TokenInfo(access_token="test-token", token_type="pat")
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path)
        client = SmartThingsClient(token_manager=manager, simulated=True)
        status = client.get_device_status(self.device_id)
        self.assertIsInstance(status, DeviceStatus)
        self.assertEqual(status.device_id, self.device_id)
        self.assertEqual(status.model, "VR7MD97716G")
        self.assertEqual(status.operating_state, "idle")
        self.assertEqual(status.battery, 100)
        self.assertTrue(status.is_online)
        self.assertIn("VR7MD97716G", status.summary())

    def test_client_retry_on_401_unauthorized(self) -> None:
        """HTTP 401 Unauthorized 발생 시 토큰 갱신 및 1회 재시도 동작 검증."""
        token_info = TokenInfo(access_token="expired-token", token_type="pat")
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path)
        client = SmartThingsClient(token_manager=manager, simulated=False)
        valid_payload = json.dumps({"components": {"main": {
            "robotCleanerOperatingState": {"operatingState": {"value": "cleaning"}},
            "battery": {"battery": {"value": 85}}, "switch": {"switch": {"value": "on"}},
        }}}).encode("utf-8")
        resp_ok = MagicMock()
        resp_ok.read.return_value = valid_payload
        resp_ok.__enter__.return_value = resp_ok
        http_401 = urllib.error.HTTPError(
            url="http://example.com", code=401, msg="Unauthorized", hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error": "invalid_token"}'),
        )
        with patch("urllib.request.urlopen", side_effect=[http_401, resp_ok]):
            status = client.get_device_status(self.device_id)
            self.assertEqual(status.operating_state, "cleaning")
            self.assertEqual(status.battery, 85)
        with patch("urllib.request.urlopen", side_effect=[http_401, http_401]):
            with self.assertRaises(SmartThingsAuthError):
                client.get_device_status(self.device_id)

    def test_client_network_error_raises_smartthings_api_error(self) -> None:
        """네트워크 연결 실패 시 SmartThingsApiError 발생 검증."""
        token_info = TokenInfo(access_token="test-token", token_type="pat")
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path)
        client = SmartThingsClient(token_manager=manager, simulated=False)
        url_error = urllib.error.URLError("[Errno 11001] getaddrinfo failed")
        with patch("urllib.request.urlopen", side_effect=url_error):
            with self.assertRaises(SmartThingsApiError):
                client.get_device_status(self.device_id)

    def test_client_execute_commands_simulated(self) -> None:
        """시뮬레이션 모드에서 명령 전송 성공 검증 (LB-001)."""
        token_info = TokenInfo(access_token="test-token", token_type="pat")
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path)
        client = SmartThingsClient(token_manager=manager, simulated=True)
        self.assertEqual(client.start_cleaning(self.device_id)["results"][0]["status"], "COMPLETED")
        self.assertEqual(client.stop_cleaning(self.device_id)["results"][0]["status"], "COMPLETED")
        self.assertEqual(client.return_to_home(self.device_id)["results"][0]["status"], "COMPLETED")

    def test_client_start_and_stop_cleaning_payloads(self) -> None:
        """start_cleaning, return_to_home의 실제 HTTP 요청 페이로드 검증 (REQ-005, REQ-016)."""
        token_info = TokenInfo(access_token="test-token", token_type="pat")
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path)
        client = SmartThingsClient(token_manager=manager, simulated=False)
        captured_reqs: list[urllib.request.Request] = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"results": [{"id": "cmd-1", "status": "COMPLETED"}]}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        def fake_urlopen(req: urllib.request.Request, timeout: int = 10) -> MagicMock:
            captured_reqs.append(req)
            return mock_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res_start = client.start_cleaning(self.device_id)
            self.assertEqual(res_start["results"][0]["status"], "COMPLETED")
            req1 = captured_reqs[0]
            self.assertEqual(req1.get_method(), "POST")
            self.assertIn("/devices/03431570-5acf-f89e-7b39-275ce15d3258/commands", req1.full_url)
            data1 = json.loads(req1.data.decode("utf-8"))  # type: ignore[union-attr]
            self.assertEqual(data1["commands"][0]["command"], "start")
            self.assertEqual(data1["commands"][0]["capability"], "samsungce.robotCleanerOperatingState")

            res_stop = client.stop_cleaning(self.device_id)
            self.assertEqual(res_stop["results"][0]["status"], "COMPLETED")
            req2 = captured_reqs[1]
            data2 = json.loads(req2.data.decode("utf-8"))  # type: ignore[union-attr]
            self.assertEqual(data2["commands"][0]["command"], "returnToHome")
            self.assertEqual(data2["commands"][0]["capability"], "samsungce.robotCleanerOperatingState")

    def test_client_execute_commands_401_retry(self) -> None:
        """명령 전송 시 401 Unauthorized 발생 시 토큰 갱신 및 1회 재시도 검증."""
        token_info = TokenInfo(access_token="expired-token", token_type="pat")
        manager = SmartThingsTokenManager(token_info=token_info, log_file=self.log_path)
        client = SmartThingsClient(token_manager=manager, simulated=False)
        resp_ok = MagicMock()
        resp_payload = json.dumps({"results": [{"id": "cmd-retry", "status": "COMPLETED"}]}).encode("utf-8")
        resp_ok.read.return_value = resp_payload
        resp_ok.__enter__.return_value = resp_ok
        http_401 = urllib.error.HTTPError(
            url="http://example.com", code=401, msg="Unauthorized", hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"error": "unauthorized"}'),
        )
        with patch("urllib.request.urlopen", side_effect=[http_401, resp_ok]):
            self.assertEqual(client.start_cleaning(self.device_id)["results"][0]["status"], "COMPLETED")
        with patch("urllib.request.urlopen", side_effect=[http_401, http_401]):
            with self.assertRaises(SmartThingsAuthError):
                client.start_cleaning(self.device_id)

    def test_verify_command_execution_success(self) -> None:
        """verify_smartthings_connection 함수가 정상 동작하고 로그를 남기는지 검증 (done_when)."""
        ret = verify_smartthings_connection(device_id=self.device_id, log_file=self.log_path, simulated=True)
        self.assertEqual(ret, 0)
        self.assertTrue(self.log_path.is_file())
        log_text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("토큰 만료 감지됨", log_text)
        self.assertIn("토큰 갱신 완료", log_text)


if __name__ == "__main__":
    unittest.main()
