"""미니PC 부팅 자동 시작 배포 스크립트 및 XML 정의 단위/통합 테스트."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from typing import Any

from server.deploy.verify_autostart import (
    check_server_health,
    generate_task_xml,
    main,
    validate_task_xml,
    verify_deployment,
)


class MockHTTPResponse:
    """헬스체크 검증을 위한 Mock HTTP 응답 컨텍스트 매니저."""

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


class TestDeploy(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.temp_path = Path(self.temp_dir.name)
        self.repo_root = Path(__file__).resolve().parent.parent
        self.template_path = self.repo_root / "server" / "deploy" / "task_definition.xml"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_task_definition_xml_template_is_valid(self) -> None:
        """기본 작업 정의 템플릿이 모든 자동 시작 및 복구 요구사항을 충족하는지 검증한다."""
        self.assertTrue(self.template_path.is_file(), f"템플릿 파일 부재: {self.template_path}")
        result = validate_task_xml(self.template_path)
        self.assertTrue(result.is_valid, f"검증 실패 원인: {result.errors}")
        self.assertEqual(result.errors, [])

        details = result.details
        self.assertTrue(details.get("boot_trigger_enabled"))
        self.assertIn("s-1-5-18", str(details.get("user_id")).lower())
        self.assertEqual(str(details.get("run_level")).lower(), "highestavailable")
        self.assertEqual(details.get("execution_time_limit"), "PT0S")
        self.assertEqual(details.get("restart_interval"), "PT1M")
        self.assertEqual(details.get("restart_count"), "3")
        self.assertEqual(details.get("arguments"), "-m server")

    def test_generate_task_xml(self) -> None:
        """템플릿 경로 치환 및 인스턴스 XML 파일 생성 동작을 검증한다."""
        out_xml = self.temp_path / "generated_task.xml"
        fake_python = r"C:\TestEnv\python.exe"
        fake_root = r"C:\TestEnv\Project"

        resolved = generate_task_xml(
            template_path=self.template_path,
            python_path=fake_python,
            project_root=fake_root,
            output_path=out_xml,
        )

        self.assertTrue(out_xml.is_file())
        self.assertIn(fake_python, resolved)
        self.assertIn(fake_root, resolved)

        reloaded_result = validate_task_xml(out_xml)
        self.assertTrue(reloaded_result.is_valid)

    def test_generate_task_xml_invalid_template(self) -> None:
        """존재하지 않는 템플릿 경로에 대해 FileNotFoundError가 발생하는지 검증한다."""
        with self.assertRaises(FileNotFoundError):
            generate_task_xml(
                template_path=self.temp_path / "non_existent.xml",
                python_path=sys.executable,
                project_root=self.repo_root,
            )

    def test_validate_task_xml_malformed_syntax(self) -> None:
        """잘못된 XML 구문 입력 시 is_valid=False 및 오류 메시지를 반환한다."""
        result = validate_task_xml("<Task><Unclosed></Task>")
        self.assertFalse(result.is_valid)
        self.assertTrue(any("파싱 실패" in err for err in result.errors))

    def test_validate_task_xml_missing_boot_trigger(self) -> None:
        """BootTrigger가 누락된 경우 실패로 처리된다."""
        xml_without_boot = """<?xml version="1.0" encoding="UTF-8"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
          <Principals><Principal><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
          <Settings><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
          <Actions><Exec><Command>python.exe</Command><Arguments>-m server</Arguments></Exec></Actions>
        </Task>"""
        res = validate_task_xml(xml_without_boot)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("BootTrigger" in err for err in res.errors))

    def test_validate_task_xml_disabled_boot_trigger(self) -> None:
        """BootTrigger가 Enabled=false인 경우 실패로 처리된다."""
        xml_disabled = """<?xml version="1.0" encoding="UTF-8"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Triggers><BootTrigger><Enabled>false</Enabled></BootTrigger></Triggers>
          <Principals><Principal><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
          <Settings><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
          <Actions><Exec><Command>python.exe</Command><Arguments>-m server</Arguments></Exec></Actions>
        </Task>"""
        res = validate_task_xml(xml_disabled)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("비활성화" in err for err in res.errors))

    def test_validate_task_xml_invalid_principal(self) -> None:
        """SYSTEM 계정이 아니거나 관리자 권한이 아닌 경우 실패로 처리된다."""
        xml_user = """<?xml version="1.0" encoding="UTF-8"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
          <Principals><Principal><UserId>MyUser</UserId><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
          <Settings><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
          <Actions><Exec><Command>python.exe</Command><Arguments>-m server</Arguments></Exec></Actions>
        </Task>"""
        res = validate_task_xml(xml_user)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("SYSTEM" in err for err in res.errors))
        self.assertTrue(any("HighestAvailable" in err for err in res.errors))

    def test_validate_task_xml_invalid_settings(self) -> None:
        """무제한 실행(PT0S)이 아니거나 장애 재시작 정책이 없는 경우 실패로 처리된다."""
        xml_bad_settings = """<?xml version="1.0" encoding="UTF-8"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
          <Principals><Principal><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
          <Settings><ExecutionTimeLimit>PT72H</ExecutionTimeLimit></Settings>
          <Actions><Exec><Command>python.exe</Command><Arguments>-m server</Arguments></Exec></Actions>
        </Task>"""
        res = validate_task_xml(xml_bad_settings)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("PT0S" in err for err in res.errors))
        self.assertTrue(any("RestartOnFailure" in err for err in res.errors))

    def test_validate_task_xml_invalid_action(self) -> None:
        """실행 대상 인자에 -m server가 누락된 경우 실패로 처리된다."""
        xml_bad_action = """<?xml version="1.0" encoding="UTF-8"?>
        <Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
          <Triggers><BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
          <Principals><Principal><UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
          <Settings><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
          <Actions><Exec><Command>python.exe</Command><Arguments>run_other.py</Arguments></Exec></Actions>
        </Task>"""
        res = validate_task_xml(xml_bad_action)
        self.assertFalse(res.is_valid)
        self.assertTrue(any("-m server" in err for err in res.errors))

    def test_check_server_health_success_and_failure(self) -> None:
        """헬스체크 정상 200 응답 및 HTTP/네트워크 오류 처리를 검증한다."""
        # 1. 미실행/접근 불가능 포트 연결 실패 처리 확인
        fail_res = check_server_health(host="127.0.0.1", port=59999, timeout=0.5)
        self.assertFalse(fail_res["healthy"])
        self.assertIsNotNone(fail_res["error"])

        # 2. Mock 200 OK 응답 확인
        def mock_ok_urlopen(req: Any, timeout: float = 3.0) -> MockHTTPResponse:
            body = json.dumps({"status": "ok", "service": "smart-things-mini-pc"}).encode("utf-8")
            return MockHTTPResponse(200, body)

        ok_res = check_server_health(host="127.0.0.1", port=8080, urlopen_fn=mock_ok_urlopen)
        self.assertTrue(ok_res["healthy"])
        self.assertEqual(ok_res["status_code"], 200)
        self.assertEqual(ok_res["data"].get("status"), "ok")
        self.assertEqual(ok_res["data"].get("service"), "smart-things-mini-pc")

        # 3. HTTPError (500) 처리 확인
        def mock_err_urlopen(req: Any, timeout: float = 3.0) -> MockHTTPResponse:
            raise urllib.error.HTTPError(req.full_url, 500, "Internal Server Error", hdrs=None, fp=None)  # type: ignore[arg-type]

        err_res = check_server_health(host="127.0.0.1", port=8080, urlopen_fn=mock_err_urlopen)
        self.assertFalse(err_res["healthy"])
        self.assertEqual(err_res["status_code"], 500)

    def test_verify_deployment_overall(self) -> None:
        """배포 구성 요소 일괄 검증 함수가 성공적으로 완료되는지 검증한다."""
        ok = verify_deployment(template_path=self.template_path, check_health=False)
        self.assertTrue(ok)

        # 헬스체크 포함 시 정상 통과 검증
        def mock_ok_urlopen(req: Any, timeout: float = 3.0) -> MockHTTPResponse:
            body = json.dumps({"status": "ok", "service": "smart-things-mini-pc"}).encode("utf-8")
            return MockHTTPResponse(200, body)

        ok_with_health = verify_deployment(
            template_path=self.template_path,
            check_health=True,
            urlopen_fn=mock_ok_urlopen,
        )
        self.assertTrue(ok_with_health)

    def test_powershell_scripts_dry_run(self) -> None:
        """PowerShell 등록/해제 스크립트가 -DryRun 모드에서 문법 오류 없이 0으로 종료되는지 검증한다."""
        deploy_dir = self.repo_root / "server" / "deploy"
        reg_ps = deploy_dir / "register_task.ps1"
        unreg_ps = deploy_dir / "unregister_task.ps1"
        out_xml = self.temp_path / "ps_test_out.xml"

        self.assertTrue(reg_ps.is_file())
        self.assertTrue(unreg_ps.is_file())

        cmd_reg = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(reg_ps),
            "-OutputXmlPath",
            str(out_xml),
            "-DryRun",
        ]
        proc_reg = subprocess.run(cmd_reg, capture_output=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(proc_reg.returncode, 0, f"register stderr: {proc_reg.stderr}")
        self.assertTrue(out_xml.is_file(), "DryRun 중 XML 인스턴스 파일이 생성되어야 합니다.")
        self.assertIn('encoding="UTF-16"', out_xml.read_text(encoding="utf-16"))

        cmd_unreg = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(unreg_ps),
            "-DryRun",
        ]
        proc_unreg = subprocess.run(cmd_unreg, capture_output=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(proc_unreg.returncode, 0, f"unregister stderr: {proc_unreg.stderr}")

    def test_cli_entry_point(self) -> None:
        """verify_autostart CLI 옵션들의 정상/비정상 종료 코드를 검증한다."""
        code_ok = main(["--check-xml", str(self.template_path)])
        self.assertEqual(code_ok, 0)

        bad_xml = self.temp_path / "bad.xml"
        bad_xml.write_text("<Invalid />", encoding="utf-8")
        code_bad = main(["--check-xml", str(bad_xml)])
        self.assertEqual(code_bad, 1)

        gen_out = self.temp_path / "cli_gen.xml"
        code_gen = main(
            [
                "--generate-xml",
                "--check-xml",
                str(self.template_path),
                "--python",
                sys.executable,
                "--root",
                str(self.repo_root),
                "--out",
                str(gen_out),
            ]
        )
        self.assertEqual(code_gen, 0)
        self.assertTrue(gen_out.is_file())


if __name__ == "__main__":
    unittest.main()
