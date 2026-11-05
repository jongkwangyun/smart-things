from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from server.smartthings.auth import (
    SmartThingsTokenManager,
    TokenInfo,
    load_token_from_file,
    save_token_to_file,
)
from server.smartthings.client import SmartThingsClient
from server.time_utils import now_kst
from tools.setup_smartthings_oauth import exchange_oauth_code


class TestSmartThingsOAuthRolling(unittest.TestCase):
    """스마트싱스 OAuth 2.0 단일 사용(single-use) 리프레시 토큰 롤링 영구 저장 및 자동 갱신 테스트 (LB-046)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.token_file = Path(self.temp_dir.name) / "st_token.json"
        self.log_file = Path(self.temp_dir.name) / "smartthings.log"
        self.env_file = Path(self.temp_dir.name) / ".env.test"
        self.env_file.write_text(
            "SMARTTHINGS_TOKEN=old_env_access_token\n"
            "SMARTTHINGS_REFRESH_TOKEN=old_env_refresh_token\n"
            "SMARTTHINGS_CLIENT_ID=test_client_id_123\n"
            "SMARTTHINGS_CLIENT_SECRET=test_client_secret_xyz\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_token_storage_atomic_save_and_load(self) -> None:
        """토큰 정보 원자적 파일 저장 및 역직렬화 복원 검증."""
        self.assertIsNone(load_token_from_file(self.token_file))

        expires = now_kst() + timedelta(hours=24)
        info = TokenInfo(
            access_token="initial_oauth_access_tok",
            token_type="oauth",
            refresh_token="initial_oauth_refresh_tok",
            expires_at=expires,
            client_id="client_id_val",
            client_secret="client_secret_val",
        )
        save_token_to_file(info, self.token_file)
        self.assertTrue(self.token_file.is_file())

        loaded = load_token_from_file(self.token_file)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.access_token, "initial_oauth_access_tok")
        self.assertEqual(loaded.token_type, "oauth")
        self.assertEqual(loaded.refresh_token, "initial_oauth_refresh_tok")
        self.assertEqual(loaded.client_id, "client_id_val")
        self.assertEqual(loaded.client_secret, "client_secret_val")
        self.assertIsNotNone(loaded.expires_at)
        assert loaded.expires_at is not None
        self.assertEqual(int(loaded.expires_at.timestamp()), int(expires.timestamp()))

        # 손상된 JSON 파싱 시 None 안전 반환
        self.token_file.write_text("{invalid json", encoding="utf-8")
        self.assertIsNone(load_token_from_file(self.token_file))

    def test_continuous_single_use_token_rotation_and_persistence(self) -> None:
        """Single-use 롤링 리프레시 토큰의 연속 3회 갱신 및 로컬 영구 파일 저장 검증."""
        used_tokens: set[str] = set()
        current_valid_refresh = "refresh_token_v0"

        def simulated_oauth_endpoint(req: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal current_valid_refresh
            # req.data 파싱
            body_str = req.data.decode("utf-8")
            from urllib.parse import parse_qs

            parsed = parse_qs(body_str)
            sent_refresh = parsed.get("refresh_token", [""])[0]

            if sent_refresh in used_tokens:
                raise RuntimeError("단일 사용(single-use) 리프레시 토큰이 이미 소비되어 무효화되었습니다.")
            if sent_refresh != current_valid_refresh:
                raise RuntimeError(f"유효하지 않은 리프레시 토큰: {sent_refresh}")

            used_tokens.add(sent_refresh)
            # 회전된 새 토큰 번호 부여
            idx = len(used_tokens)
            current_valid_refresh = f"refresh_token_v{idx}"

            res_data = {
                "access_token": f"access_token_v{idx}",
                "refresh_token": current_valid_refresh,
                "expires_in": 3600,
            }
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(res_data).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_resp.__exit__.return_value = None
            return mock_resp

        init_info = TokenInfo(
            access_token="access_token_v0",
            token_type="oauth",
            refresh_token=current_valid_refresh,
            client_id="test_client_id_123",
            client_secret="test_client_secret_xyz",
            expires_at=now_kst() - timedelta(seconds=10),  # 만료 상태
        )
        manager = SmartThingsTokenManager(
            token_info=init_info,
            log_file=self.log_file,
            token_file=self.token_file,
            env_path=self.env_file,
        )

        with patch("urllib.request.urlopen", side_effect=simulated_oauth_endpoint):
            # 1회차 롤링 갱신
            token1 = manager.get_token()
            self.assertEqual(token1, "access_token_v1")
            self.assertEqual(manager.token_info.refresh_token, "refresh_token_v1")
            saved1 = load_token_from_file(self.token_file)
            self.assertIsNotNone(saved1)
            assert saved1 is not None
            self.assertEqual(saved1.access_token, "access_token_v1")
            self.assertEqual(saved1.refresh_token, "refresh_token_v1")

            # 2회차 롤링 갱신
            manager.force_expire()
            token2 = manager.get_token()
            self.assertEqual(token2, "access_token_v2")
            self.assertEqual(manager.token_info.refresh_token, "refresh_token_v2")
            saved2 = load_token_from_file(self.token_file)
            self.assertIsNotNone(saved2)
            assert saved2 is not None
            self.assertEqual(saved2.refresh_token, "refresh_token_v2")

            # 3회차 롤링 갱신
            manager.force_expire()
            token3 = manager.get_token()
            self.assertEqual(token3, "access_token_v3")
            self.assertEqual(manager.token_info.refresh_token, "refresh_token_v3")
            saved3 = load_token_from_file(self.token_file)
            self.assertIsNotNone(saved3)
            assert saved3 is not None
            self.assertEqual(saved3.refresh_token, "refresh_token_v3")

        # 이전 토큰 3개가 모두 성공적으로 회전 및 무효화 기록되었는지 검증
        self.assertEqual(len(used_tokens), 3)

    def test_from_env_restores_latest_rolling_token_from_file(self) -> None:
        """서버 재시작 시 .env의 옛날 토큰 대신 파일의 최신 롤링 토큰이 복원되는지 검증."""
        latest_info = TokenInfo(
            access_token="latest_rolled_access_999",
            token_type="oauth",
            refresh_token="latest_rolled_refresh_999",
            expires_at=now_kst() + timedelta(hours=12),
            client_id="test_client_id_123",
            client_secret="test_client_secret_xyz",
        )
        save_token_to_file(latest_info, self.token_file)

        # 재시작 시뮬레이션: from_env 호출
        restored_manager = SmartThingsTokenManager.from_env(
            env_path=self.env_file,
            log_file=self.log_file,
            token_file=self.token_file,
        )

        self.assertEqual(restored_manager.token_info.access_token, "latest_rolled_access_999")
        self.assertEqual(restored_manager.token_info.refresh_token, "latest_rolled_refresh_999")
        self.assertEqual(restored_manager.token_info.token_type, "oauth")

    def test_preemptive_expiration_detection(self) -> None:
        """만료 1시간 전 선제적 감지 (buffer_seconds=3600) 동작 검증."""
        # 현재로부터 50분 후 만료 예정인 토큰
        exp_in_50m = now_kst() + timedelta(minutes=50)
        token_info = TokenInfo(
            access_token="tok_valid_for_50m",
            token_type="oauth",
            refresh_token="ref_valid_for_50m",
            expires_at=exp_in_50m,
        )
        manager = SmartThingsTokenManager(
            token_info=token_info,
            log_file=self.log_file,
            token_file=self.token_file,
        )

        # 기본 버퍼(60초) 기준으로는 아직 만료되지 않음
        self.assertFalse(manager.is_expired(buffer_seconds=60))

        # 1시간(3600초) 선제 감지 기준으로는 만료 상태로 판정되어야 함
        self.assertTrue(manager.is_expired(buffer_seconds=3600))

    def test_client_401_triggers_oauth_refresh_and_disk_save(self) -> None:
        """SmartThingsClient 401 오류 시 토큰 자동 갱신 및 디스크 영구 저장 연계 검증."""
        init_info = TokenInfo(
            access_token="expiring_token_401",
            token_type="oauth",
            refresh_token="rot_refresh_before_401",
            client_id="test_client_id_123",
            client_secret="test_client_secret_xyz",
            expires_at=now_kst() + timedelta(hours=1),
        )
        save_token_to_file(init_info, self.token_file)

        def mock_refresh_handler(curr: TokenInfo) -> TokenInfo:
            return TokenInfo(
                access_token="new_fresh_token_after_401",
                token_type="oauth",
                refresh_token="new_fresh_refresh_after_401",
                client_id=curr.client_id,
                client_secret=curr.client_secret,
                expires_at=now_kst() + timedelta(hours=24),
            )

        token_mgr = SmartThingsTokenManager(
            token_info=init_info,
            log_file=self.log_file,
            token_file=self.token_file,
            refresh_handler=mock_refresh_handler,
        )
        client = SmartThingsClient(token_manager=token_mgr)

        # 401 오류 후 재시도 시 성공하는 시뮬레이션
        call_count = 0

        def fake_request_json(url: str, token: str, method: str = "GET", payload: Any = None) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                from server.smartthings.client import SmartThingsAuthError
                raise SmartThingsAuthError("401 Unauthorized")
            return {"components": {"main": {"robotCleanerOperatingState": {"operatingState": {"value": "cleaning"}}}}}

        with patch.object(client, "_request_json", side_effect=fake_request_json):
            res = client.get_device_status("dummy-device-id")
            self.assertEqual(res.operating_state, "cleaning")
            self.assertEqual(call_count, 2)

        # 디스크 파일에 401 후 갱신된 새 토큰이 즉시 저장되었는지 검증
        saved = load_token_from_file(self.token_file)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.access_token, "new_fresh_token_after_401")
        self.assertEqual(saved.refresh_token, "new_fresh_refresh_after_401")

    def test_setup_oauth_code_exchange_helper(self) -> None:
        """tools/setup_smartthings_oauth.py 교환 함수 정상 동작 검증."""
        fake_resp = {
            "access_token": "oauth_access_initial_setup",
            "refresh_token": "oauth_refresh_initial_setup",
            "expires_in": 86400,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_resp).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        with patch("urllib.request.urlopen", return_value=mock_resp):
            info = exchange_oauth_code(
                client_id="my_client_id",
                client_secret="my_client_secret",
                code="my_auth_code",
                redirect_uri="https://httpbin.org/get",
            )
            self.assertEqual(info.access_token, "oauth_access_initial_setup")
            self.assertEqual(info.refresh_token, "oauth_refresh_initial_setup")
            self.assertEqual(info.token_type, "oauth")
            self.assertEqual(info.client_id, "my_client_id")
            self.assertEqual(info.client_secret, "my_client_secret")

    def test_gitignore_protects_token_storage(self) -> None:
        """server/data/st_token.json 이 gitignore에 의해 저장소 추적에서 보호되는지 검증."""
        result = subprocess.run(
            ["git", "check-ignore", "server/data/st_token.json"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("server/data/", result.stdout)
