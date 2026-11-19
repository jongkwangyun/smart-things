from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from server.smartthings.auth import SmartThingsTokenManager, TokenInfo, TokenRefreshError
from server.smartthings.token_storage import (
    find_cli_credentials_file,
    find_smartthings_cli_executable,
    load_token_from_file,
    parse_cli_credentials,
    sync_token_from_cli,
)
from server.time_utils import KST, now_kst
from tools import login_with_cli, setup_smartthings_oauth


class TestCliTokenSync(unittest.TestCase):
    """Windows/Linux(Ubuntu) SmartThings CLI 자격 증명 동기화 및 OAuth 도구 검증 (LB-062)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.token_file = self.root / "server/data/st_token.json"
        self.log_file = self.root / "smartthings.log"
        self.env_file = self.root / ".env"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_sync_from_xdg_config_home_on_linux(self) -> None:
        """XDG_CONFIG_HOME/@smartthings/cli/credentials.json 에서 토큰을 읽어 st_token.json 에 동기화한다."""
        xdg_config = self.root / "xdg_config"
        cred_path = xdg_config / "@smartthings/cli/credentials.json"
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(
            json.dumps({
                "default:api.smartthings.com": {
                    "accessToken": "linux_xdg_access_123",
                    "refreshToken": "linux_xdg_refresh_456",
                    "expires": "2026-11-20T03:00:00Z",
                }
            }),
            encoding="utf-8",
        )

        env = {"XDG_CONFIG_HOME": str(xdg_config), "HOME": str(self.root / "home")}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=None),
        ):
            self.assertEqual(find_cli_credentials_file(), cred_path)
            info = sync_token_from_cli(self.token_file)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.access_token, "linux_xdg_access_123")
        self.assertEqual(info.refresh_token, "linux_xdg_refresh_456")
        self.assertEqual(info.token_type, "oauth")
        self.assertEqual(info.client_id, "smartthings-cli")
        self.assertIsNotNone(info.expires_at)
        assert info.expires_at is not None
        self.assertEqual(info.expires_at.utcoffset(), KST.utcoffset(None))
        self.assertEqual(info.expires_at.hour, 12)

        saved = load_token_from_file(self.token_file)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.access_token, "linux_xdg_access_123")
        self.assertEqual(saved.refresh_token, "linux_xdg_refresh_456")

    def test_sync_from_default_dot_config_and_xdg_data_home_fallback(self) -> None:
        """XDG_CONFIG_HOME 미설정 시 ~/.config 및 XDG_DATA_HOME 폴백 경로를 탐색한다."""
        fake_home = self.root / "fake_home"
        default_cred = fake_home / ".config/@smartthings/cli/credentials.json"
        default_cred.parent.mkdir(parents=True, exist_ok=True)
        default_cred.write_text(
            json.dumps({
                "default:api.smartthings.com": {"accessToken": "home_cfg_tok", "refreshToken": "home_cfg_ref"},
            }),
            encoding="utf-8",
        )

        with (
            patch.dict(os.environ, {"HOME": str(fake_home)}, clear=True),
            patch("pathlib.Path.home", return_value=fake_home),
            patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=None),
        ):
            self.assertEqual(find_cli_credentials_file(), default_cred)
            info = sync_token_from_cli(self.token_file)
            self.assertIsNotNone(info)
            assert info is not None
            self.assertEqual(info.access_token, "home_cfg_tok")

        # ~/.config 없을 때 XDG_DATA_HOME 폴백 확인
        default_cred.unlink()
        xdg_data = self.root / "xdg_data"
        data_cred = xdg_data / "@smartthings/cli/credentials.json"
        data_cred.parent.mkdir(parents=True, exist_ok=True)
        data_cred.write_text(
            json.dumps({"default:api.smartthings.com": {"accessToken": "xdg_data_tok"}}),
            encoding="utf-8",
        )
        with (
            patch.dict(os.environ, {"HOME": str(fake_home), "XDG_DATA_HOME": str(xdg_data)}, clear=True),
            patch("pathlib.Path.home", return_value=fake_home),
        ):
            self.assertEqual(find_cli_credentials_file(), data_cred)

    def test_sync_preserves_windows_localappdata_path_and_runs_cli_refresh(self) -> None:
        """Windows LOCALAPPDATA 경로 지원 및 CLI 실행 시 기존 PAT 환경 변수 제거 검증."""
        local_app = self.root / "local_appdata"
        cred_path = local_app / "@smartthings/cli/Data/credentials.json"
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(
            json.dumps({"default:api.smartthings.com": {"accessToken": "stale_cli_tok", "refreshToken": "ref_1"}}),
            encoding="utf-8",
        )
        cli_bin = self.root / "smartthings"
        cli_bin.write_text("#!/bin/sh\n", encoding="utf-8")

        captured_env: dict[str, str] = {}

        def fake_run(cmd: list[str], **kwargs: Any) -> MagicMock:
            nonlocal captured_env
            captured_env = dict(kwargs.get("env", {}))
            self.assertEqual(cmd, [str(cli_bin), "devices", "-j"])
            cred_path.write_text(
                json.dumps({
                    "default:api.smartthings.com": {"accessToken": "refreshed_cli_tok", "refreshToken": "ref_2"},
                }),
                encoding="utf-8",
            )
            return MagicMock(returncode=0)

        env = {
            "LOCALAPPDATA": str(local_app),
            "SMARTTHINGS_TOKEN": "old_pat",
            "SMARTTHINGS_PAT": "old_pat_2",
            "SMARTTHINGS_ACCESS_TOKEN": "old_pat_3",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=cli_bin),
            patch("subprocess.run", side_effect=fake_run),
        ):
            info = sync_token_from_cli(self.token_file)

        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.access_token, "refreshed_cli_tok")
        self.assertEqual(info.refresh_token, "ref_2")
        self.assertNotIn("SMARTTHINGS_TOKEN", captured_env)
        self.assertNotIn("SMARTTHINGS_PAT", captured_env)
        self.assertNotIn("SMARTTHINGS_ACCESS_TOKEN", captured_env)

    def test_find_smartthings_cli_executable_linux_and_windows(self) -> None:
        """Linux(PROJECT_ROOT/smartthings, PATH) 및 Windows(smartthings.exe) 실행 파일 탐색 검증."""
        fake_proj = self.root / "proj"
        fake_proj.mkdir()
        linux_cli = fake_proj / "smartthings"
        linux_cli.write_text("#!/bin/sh\n", encoding="utf-8")

        with (
            patch("server.smartthings.token_storage.PROJECT_ROOT", fake_proj),
            patch("server.smartthings.token_storage.sys.platform", "linux"),
            patch("pathlib.Path.cwd", return_value=self.root),
        ):
            self.assertEqual(find_smartthings_cli_executable(), linux_cli)

        linux_cli.unlink()
        path_cli = self.root / "bin_smartthings"
        path_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        with (
            patch("server.smartthings.token_storage.PROJECT_ROOT", fake_proj),
            patch("server.smartthings.token_storage.sys.platform", "linux"),
            patch("pathlib.Path.cwd", return_value=self.root),
            patch("shutil.which", return_value=str(path_cli)),
        ):
            self.assertEqual(find_smartthings_cli_executable(), path_cli)

        win_cli = fake_proj / "smartthings.exe"
        win_cli.write_text("exe", encoding="utf-8")
        with (
            patch("server.smartthings.token_storage.PROJECT_ROOT", fake_proj),
            patch("server.smartthings.token_storage.sys.platform", "win32"),
            patch("pathlib.Path.cwd", return_value=self.root),
        ):
            self.assertEqual(find_smartthings_cli_executable(), win_cli)

        win_cli.unlink()
        win_path_cli = self.root / "bin_smartthings.exe"
        win_path_cli.write_text("exe", encoding="utf-8")
        with (
            patch("server.smartthings.token_storage.PROJECT_ROOT", fake_proj),
            patch("server.smartthings.token_storage.sys.platform", "win32"),
            patch("pathlib.Path.cwd", return_value=self.root),
            patch("shutil.which", return_value=str(win_path_cli)),
        ):
            self.assertEqual(find_smartthings_cli_executable(), win_path_cli)

    def test_token_manager_oauth_without_client_secret_uses_linux_cli_sync(self) -> None:
        """client_secret 없는 CLI OAuth 토큰 만료 시 Linux XDG CLI 동기화로 자동 갱신된다."""
        xdg_config = self.root / "xdg_cfg_mgr"
        cred_path = xdg_config / "@smartthings/cli/credentials.json"
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(
            json.dumps({
                "default:api.smartthings.com": {
                    "accessToken": "cli_synced_new_tok",
                    "refreshToken": "cli_synced_new_ref",
                    "expires": (now_kst() + timedelta(hours=12)).isoformat(),
                }
            }),
            encoding="utf-8",
        )

        expired_cli_token = TokenInfo(
            access_token="expired_cli_tok",
            token_type="oauth",
            refresh_token="old_cli_ref",
            client_id="smartthings-cli",
            expires_at=now_kst() - timedelta(minutes=5),
        )
        mgr = SmartThingsTokenManager(
            token_info=expired_cli_token,
            log_file=self.log_file,
            env_path=self.env_file,
            token_file=self.token_file,
        )

        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_config)}, clear=True),
            patch("server.smartthings.token_storage.find_smartthings_cli_executable", return_value=None),
        ):
            fresh_tok = mgr.get_token()

        self.assertEqual(fresh_tok, "cli_synced_new_tok")
        saved = load_token_from_file(self.token_file)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.access_token, "cli_synced_new_tok")

        # 자격 증명 파일도 없으면 TokenRefreshError 발생
        cred_path.unlink()
        mgr.force_expire()
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_config)}, clear=True),
            patch("pathlib.Path.home", return_value=self.root / "empty_home"),
        ):
            with self.assertRaises(TokenRefreshError):
                mgr.get_token()

    def test_parse_cli_credentials_variants_and_edge_cases(self) -> None:
        """credentials.json / keytar / 잘못된 입력 파싱 엣지 케이스 검증."""
        self.assertIsNone(parse_cli_credentials("not-json"))
        self.assertIsNone(parse_cli_credentials("[]"))
        self.assertIsNone(parse_cli_credentials(json.dumps({"default:api.smartthings.com": {"accessToken": ""}})))

        keytar_info = parse_cli_credentials(
            json.dumps({
                "clientId": "custom-cli-id",
                "token": {"access_token": "kt_acc", "refresh_token": "kt_ref"},
            })
        )
        self.assertIsNotNone(keytar_info)
        assert keytar_info is not None
        self.assertEqual(keytar_info.access_token, "kt_acc")
        self.assertEqual(keytar_info.refresh_token, "kt_ref")
        self.assertEqual(keytar_info.client_id, "custom-cli-id")

        bad_exp = parse_cli_credentials(
            json.dumps({"custom-profile": {"accessToken": "prof_tok", "expires": "invalid-date"}})
        )
        self.assertIsNotNone(bad_exp)
        assert bad_exp is not None
        self.assertEqual(bad_exp.access_token, "prof_tok")
        self.assertIsNone(bad_exp.expires_at)

    def test_login_with_cli_sync_only_and_interactive_flow(self) -> None:
        """tools/login_with_cli.py 의 --sync-only, 실행 파일 탐색, 오류 코드 처리를 검증한다."""
        xdg_config = self.root / "cli_tool_xdg"
        cred_path = xdg_config / "@smartthings/cli/credentials.json"
        cred_path.parent.mkdir(parents=True, exist_ok=True)
        cred_path.write_text(
            json.dumps({
                "default:api.smartthings.com": {
                    "accessToken": "tool_sync_access",
                    "refreshToken": "tool_sync_refresh",
                    "expires": "2026-11-20T15:00:00+09:00",
                }
            }),
            encoding="utf-8",
        )

        # 1. --sync-only 성공
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_config)}, clear=True):
            rc = login_with_cli.main(["--sync-only", "--token-file", str(self.token_file)])
        self.assertEqual(rc, 0)
        saved = load_token_from_file(self.token_file)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.access_token, "tool_sync_access")

        # 2. 실행 파일 없음 -> exit 1
        with patch("tools.login_with_cli.find_smartthings_cli_executable", return_value=None):
            self.assertEqual(login_with_cli.main(["--token-file", str(self.token_file)]), 1)

        # 3. CLI 실행 성공 후 자격 증명 저장 -> exit 0
        fake_cli = self.root / "smartthings"
        fake_cli.write_text("#!/bin/sh\n", encoding="utf-8")
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"[]", b"")
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg_config), "SMARTTHINGS_TOKEN": "pat"}, clear=True),
            patch("tools.login_with_cli.find_smartthings_cli_executable", return_value=fake_cli),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
        ):
            rc2 = login_with_cli.main(["--token-file", str(self.token_file)])
        self.assertEqual(rc2, 0)
        popen_env = mock_popen.call_args.kwargs["env"]
        self.assertNotIn("SMARTTHINGS_TOKEN", popen_env)

        # 4. CLI 실행 실패 -> exit 2
        mock_proc.returncode = 1
        mock_proc.communicate.return_value = (b"", b"auth failed")
        with (
            patch("tools.login_with_cli.find_smartthings_cli_executable", return_value=fake_cli),
            patch("subprocess.Popen", return_value=mock_proc),
        ):
            self.assertEqual(login_with_cli.main(["--token-file", str(self.token_file)]), 2)

        # 5. 자격 증명 미발견 -> exit 3
        with (
            patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "missing_xdg")}, clear=True),
            patch("pathlib.Path.home", return_value=self.root / "missing_home"),
        ):
            self.assertEqual(login_with_cli.main(["--sync-only", "--token-file", str(self.token_file)]), 3)

    def test_setup_smartthings_oauth_main_on_linux(self) -> None:
        """tools/setup_smartthings_oauth.py main() 이 Linux에서 OAuth 교환 및 env 폴백을 수행한다."""
        fake_resp = {
            "access_token": "oauth_main_acc",
            "refresh_token": "oauth_main_ref",
            "expires_in": 3600,
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_resp).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None

        env = {"SMARTTHINGS_CLIENT_ID": "env_cid", "SMARTTHINGS_CLIENT_SECRET": "env_csec"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("urllib.request.urlopen", return_value=mock_resp),
        ):
            rc = setup_smartthings_oauth.main(["--code", "auth_code_1", "--token-file", str(self.token_file)])

        self.assertEqual(rc, 0)
        saved = load_token_from_file(self.token_file)
        self.assertIsNotNone(saved)
        assert saved is not None
        self.assertEqual(saved.access_token, "oauth_main_acc")
        self.assertEqual(saved.refresh_token, "oauth_main_ref")
        self.assertEqual(saved.client_id, "env_cid")
        self.assertEqual(saved.client_secret, "env_csec")

        # 필수 인자 누락(비대화형) -> exit 1
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("tools.setup_smartthings_oauth.resolve_credential", return_value=""),
            patch("sys.stdin.isatty", return_value=False),
        ):
            self.assertEqual(setup_smartthings_oauth.main(["--token-file", str(self.token_file)]), 1)


if __name__ == "__main__":
    unittest.main()
