from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from server.off_periods.auto_stop import DEFAULT_DEVICE_ID
from server.scheduler import ReservationScheduler
from server.smartthings.auth import (
    DEFAULT_OAUTH_REFRESH_BUFFER_SECONDS,
    SmartThingsTokenManager,
    TokenInfo,
    TokenRefreshError,
    load_token_from_file,
    save_token_to_file,
)
from server.smartthings.client import SmartThingsClient
from server.smartthings.token_storage import sync_token_from_cli
from server.storage import Storage
from server.time_utils import now_kst


def _close_log(logger_name: str, path: Path) -> None:
    logger = logging.getLogger(logger_name)
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename).resolve() == path.resolve():
            h.flush()
            h.close()
            logger.removeHandler(h)


class TestLB066OAuthPreemptiveAndCliGuard(unittest.TestCase):
    """LB-066: OAuth 30분(1800초) 전 선제 갱신 및 CLI 미갱신 만료 토큰 방어 검증."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.token_file = self.root / "st_token.json"
        self.log_file = self.root / "smartthings.log"
        self.sched_log = self.root / "scheduler.log"
        self.env_file = self.root / ".env"
        self.db_path = self.root / "lb066.db"
        self.xdg_config = self.root / "xdg_config"
        self.cred_path = self.xdg_config / "@smartthings/cli/credentials.json"
        self.cred_path.parent.mkdir(parents=True, exist_ok=True)
        self.cli_bin = self.root / "smartthings"
        self.cli_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    def tearDown(self) -> None:
        _close_log("server.smartthings", self.log_file)
        _close_log("server.scheduler", self.sched_log)
        self.temp_dir.cleanup()

    def _write_cli_creds(self, access: str, refresh: str, expires_delta: timedelta | None) -> None:
        entry: dict[str, Any] = {"accessToken": access, "refreshToken": refresh}
        if expires_delta is not None:
            entry["expires"] = (now_kst() + expires_delta).isoformat()
        self.cred_path.write_text(json.dumps({"default:api.smartthings.com": entry}), encoding="utf-8")

    def test_oauth_preemptive_refresh_at_25m_and_not_at_45m(self) -> None:
        """OAuth 토큰 만료 25분 전에는 get_token()이 선제 갱신하고, 45분 전에는 갱신하지 않는다."""
        self.assertEqual(DEFAULT_OAUTH_REFRESH_BUFFER_SECONDS, 1800)
        self._write_cli_creds("cli_fresh_24h", "cli_ref_24h", timedelta(hours=24))

        # 1) 만료 45분 전: is_expired(60) False, get_token()도 갱신 미호출
        oauth_45m = TokenInfo(
            access_token="oauth_45m",
            token_type="oauth",
            refresh_token="ref_45m",
            client_id="smartthings-cli",
            expires_at=now_kst() + timedelta(minutes=45),
        )
        mgr_45m = SmartThingsTokenManager(
            token_info=oauth_45m, log_file=self.log_file, env_path=self.env_file, token_file=self.token_file
        )
        with patch("server.smartthings.auth.sync_token_from_cli") as mock_sync:
            self.assertFalse(mgr_45m.is_expired())
            self.assertEqual(mgr_45m.get_token(), "oauth_45m")
            mock_sync.assert_not_called()

        # 2) 만료 25분 전: is_expired() 기본(60초)은 False지만 get_token() 기본(1800초)은 선제 갱신 수행
        oauth_25m = TokenInfo(
            access_token="oauth_25m",
            token_type="oauth",
            refresh_token="ref_25m",
            client_id="smartthings-cli",
            expires_at=now_kst() + timedelta(minutes=25),
        )
        mgr_25m = SmartThingsTokenManager(
            token_info=oauth_25m, log_file=self.log_file, env_path=self.env_file, token_file=self.token_file
        )
        self.assertFalse(mgr_25m.is_expired())
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg_config)}, clear=True),
            patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=self.cli_bin),
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as mock_run,
        ):
            tok = mgr_25m.get_token()
            mock_run.assert_called_once()
        self.assertEqual(tok, "cli_fresh_24h")
        saved = load_token_from_file(self.token_file)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.access_token, "cli_fresh_24h")

    def test_pat_default_60s_and_explicit_buffer_override(self) -> None:
        """PAT는 기존 60초 버퍼를 유지하고, OAuth도 명시적 buffer_seconds=60 전달 시 이를 존중한다."""
        pat_25m = TokenInfo(
            access_token="pat_25m",
            token_type="pat",
            expires_at=now_kst() + timedelta(minutes=25),
        )
        pat_mgr = SmartThingsTokenManager(token_info=pat_25m, log_file=self.log_file, env_path=self.env_file)
        with patch.object(pat_mgr, "refresh") as mock_refresh:
            self.assertFalse(pat_mgr.is_expired())
            self.assertEqual(pat_mgr.get_token(), "pat_25m")
            mock_refresh.assert_not_called()

        oauth_25m = TokenInfo(
            access_token="oauth_25m_explicit",
            token_type="oauth",
            refresh_token="ref_25m",
            client_id="smartthings-cli",
            expires_at=now_kst() + timedelta(minutes=25),
        )
        oauth_mgr = SmartThingsTokenManager(token_info=oauth_25m, log_file=self.log_file, env_path=self.env_file)
        with patch.object(oauth_mgr, "refresh") as mock_refresh:
            self.assertEqual(oauth_mgr.get_token(buffer_seconds=60), "oauth_25m_explicit")
            mock_refresh.assert_not_called()

    def test_reload_if_updated_with_near_expiry_oauth_still_refreshes(self) -> None:
        """st_token.json이 20분 남은 OAuth 토큰으로 교체되었어도 refresh()가 추가 OAuth 갱신을 수행한다."""
        init_tok = TokenInfo(
            access_token="init_old_tok",
            token_type="oauth",
            refresh_token="init_ref",
            client_id="smartthings-cli",
            expires_at=now_kst() + timedelta(minutes=10),
        )
        save_token_to_file(init_tok, self.token_file)
        mgr = SmartThingsTokenManager(
            token_info=init_tok, log_file=self.log_file, env_path=self.env_file, token_file=self.token_file
        )
        # 외부 파일이 20분 남은 다른 토큰으로 변경됨
        save_token_to_file(
            TokenInfo(
                access_token="external_20m_tok",
                token_type="oauth",
                refresh_token="external_ref",
                client_id="smartthings-cli",
                expires_at=now_kst() + timedelta(minutes=20),
            ),
            self.token_file,
        )
        self._write_cli_creds("cli_24h_after_reload", "cli_ref_24h", timedelta(hours=24))
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg_config)}, clear=True),
            patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=None),
        ):
            tok = mgr.get_token()
        self.assertEqual(tok, "cli_24h_after_reload")

    def test_cli_failure_returning_unchanged_expired_token_raises_error_and_preserves_file(self) -> None:
        """CLI 실패(returncode != 0 또는 TimeoutExpired)로 만료 토큰이 유지되면 TokenRefreshError가 발생한다."""
        expired_tok = TokenInfo(
            access_token="stale_expired_tok",
            token_type="oauth",
            refresh_token="stale_ref",
            client_id="smartthings-cli",
            expires_at=now_kst() - timedelta(minutes=5),
        )
        save_token_to_file(expired_tok, self.token_file)
        before_raw = self.token_file.read_text(encoding="utf-8")
        self._write_cli_creds("stale_expired_tok", "stale_ref", timedelta(minutes=-5))

        for side_eff in (MagicMock(returncode=1), subprocess.TimeoutExpired(cmd="smartthings", timeout=15)):
            mgr = SmartThingsTokenManager(
                token_info=expired_tok, log_file=self.log_file, env_path=self.env_file, token_file=self.token_file
            )
            with (
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg_config)}, clear=True),
                patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=self.cli_bin),
                patch("subprocess.run", side_effect=[side_eff]),
            ):
                self.assertIsNone(sync_token_from_cli(self.token_file))
                self.assertEqual(self.token_file.read_text(encoding="utf-8"), before_raw)

            with (
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg_config)}, clear=True),
                patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=self.cli_bin),
                patch("subprocess.run", side_effect=[side_eff]),
            ):
                with self.assertRaises(TokenRefreshError):
                    mgr.get_token()

        for h in mgr.logger.handlers:
            h.flush()
        log_text = self.log_file.read_text(encoding="utf-8")
        self.assertIn("토큰 갱신 실패", log_text)
        self.assertNotIn("토큰 갱신 완료", log_text)

    def test_refresh_oauth_rejects_unchanged_or_expired_cli_return(self) -> None:
        """sync_token_from_cli가 동일/만료 토큰을 반환하더라도 _refresh_oauth()가 TokenRefreshError를 발생시킨다."""
        cases = [
            TokenInfo("same_tok", "oauth", "ref", now_kst() - timedelta(minutes=1), "smartthings-cli"),
            TokenInfo("same_tok", "oauth", "ref", now_kst() + timedelta(minutes=20), "smartthings-cli"),
            TokenInfo("same_tok", "oauth", "ref", None, "smartthings-cli"),
            TokenInfo("different_but_expired", "oauth", "ref2", now_kst() - timedelta(seconds=30), "smartthings-cli"),
        ]
        for returned in cases:
            cur = TokenInfo(
                access_token="same_tok", token_type="oauth", refresh_token="ref",
                client_id="smartthings-cli", expires_at=now_kst() + timedelta(minutes=15),
            )
            mgr = SmartThingsTokenManager(
                token_info=cur, log_file=self.log_file, env_path=self.env_file, token_file=self.token_file
            )
            with patch("server.smartthings.auth.sync_token_from_cli", return_value=returned):
                with self.assertRaises(TokenRefreshError):
                    mgr.get_token()

        # 401 응답 후 CLI가 유효기간이 남은 동일 토큰을 돌려줘도 TokenRefreshError 발생
        unexpired_same = TokenInfo(
            "same_tok", "oauth", "ref", now_kst() + timedelta(hours=10), "smartthings-cli"
        )
        mgr_401 = SmartThingsTokenManager(
            token_info=unexpired_same, log_file=self.log_file, env_path=self.env_file, token_file=self.token_file
        )
        with patch("server.smartthings.auth.sync_token_from_cli", return_value=unexpired_same):
            with self.assertRaises(TokenRefreshError):
                mgr_401.refresh(reason="401_unauthorized")

    def test_client_401_retry_no_double_refresh_and_scheduler_backoff_on_cli_failure(self) -> None:
        """401 재시도 후 1시간 토큰은 이중 갱신하지 않고, CLI 미갱신 실패는 AuthBackoffTracker에 기록된다."""
        # 1) 401 후 1시간(3600s > 1800s) 토큰 반환 시 get_token() 이중 갱신 없음
        tok = TokenInfo("oauth_old_401", "oauth", "ref_1", now_kst() + timedelta(hours=2), "cid", "csec")
        mgr = SmartThingsTokenManager(token_info=tok, log_file=self.log_file, env_path=self.env_file)
        client = SmartThingsClient(token_manager=mgr, simulated=False)
        http_401 = urllib.error.HTTPError(
            url="https://api.smartthings.com", code=401, msg="Unauthorized", hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(b"{}"),
        )
        oauth_resp = MagicMock()
        oauth_resp.read.return_value = json.dumps(
            {"access_token": "oauth_new_1h", "refresh_token": "ref_2", "expires_in": 3600}
        ).encode("utf-8")
        oauth_resp.__enter__.return_value = oauth_resp
        ok_status = MagicMock()
        ok_status.read.return_value = json.dumps(
            {"components": {"main": {"robotCleanerOperatingState": {"operatingState": {"value": "idle"}}}}}
        ).encode("utf-8")
        ok_status.__enter__.return_value = ok_status

        with patch("urllib.request.urlopen", side_effect=[http_401, oauth_resp, ok_status]) as m_open:
            st = client.get_device_status(DEFAULT_DEVICE_ID)
            self.assertEqual(st.operating_state, "idle")
            self.assertEqual(m_open.call_count, 3)

        # 2) 만료 20분 전 OAuth 토큰 + CLI 실패 시 스케줄러 AuthBackoffTracker 백오프 진입
        self._write_cli_creds("cli_20m_stale", "cli_ref_stale", timedelta(minutes=20))
        stale_mgr = SmartThingsTokenManager(
            token_info=TokenInfo(
                access_token="cli_20m_stale", token_type="oauth", refresh_token="cli_ref_stale",
                client_id="smartthings-cli", expires_at=now_kst() + timedelta(minutes=20),
            ),
            log_file=self.log_file,
            env_path=self.env_file,
            token_file=self.token_file,
        )
        stale_client = SmartThingsClient(token_manager=stale_mgr, simulated=False)
        storage = Storage(self.db_path)
        try:
            sched = ReservationScheduler(
                storage=storage, client=stale_client, device_id=DEFAULT_DEVICE_ID,
                log_file=self.sched_log, auth_backoff_base=10.0,
            )
            with (
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg_config)}, clear=True),
                patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=self.cli_bin),
                patch("subprocess.run", return_value=MagicMock(returncode=1)),
            ):
                sched.step(now_kst())
            self.assertEqual(sched.auth_backoff.failure_count, 1)
            self.assertTrue(sched.auth_backoff.is_backing_off)
            self.assertIn("갱신되지 않았거나", sched.auth_backoff.last_auth_error or "")
        finally:
            storage.close()


if __name__ == "__main__":
    unittest.main()
