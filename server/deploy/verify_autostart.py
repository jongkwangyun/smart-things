"""미니PC 부팅 자동 시작 작업 정의 검증 및 헬스체크 모듈.

REQ-026 및 미니PC 재부팅 시 수동 조작 없는 자동 복구(must)를 보장하기 위해
작업 스케줄러 XML 정의, 스크립트 존재 여부, 서버 상태 확인 엔드포인트를 검증합니다.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TASK_SCHEMA_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
SYSTEM_ACCOUNT_IDENTIFIERS = ("s-1-5-18", "nt authority\\system", "system")


@dataclass
class ValidationResult:
    """작업 스케줄러 XML 검증 결과."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def _strip_ns(tag: str) -> str:
    """XML 태그에서 네임스페이스를 제거한다."""
    return tag.split("}")[-1] if "}" in tag else tag


def _find_child(parent: ET.Element, name: str) -> ET.Element | None:
    """네임스페이스에 구애받지 않고 첫 번째 자식 요소를 찾는다."""
    name_lower = name.lower()
    for child in parent:
        if _strip_ns(child.tag).lower() == name_lower:
            return child
    return None


def _get_child_text(parent: ET.Element, name: str, default: str = "") -> str:
    """자식 요소의 텍스트 값을 안전하게 조회한다."""
    child = _find_child(parent, name)
    return child.text.strip() if child is not None and child.text else default


def validate_task_xml(source: str | bytes | Path | ET.Element) -> ValidationResult:
    """작업 스케줄러 XML 정의의 요구사항 준수 여부를 검증한다."""
    errors: list[str] = []
    details: dict[str, Any] = {}

    if isinstance(source, ET.Element):
        root = source
    elif isinstance(source, Path):
        try:
            root = ET.parse(source).getroot()
        except Exception as e:
            return ValidationResult(is_valid=False, errors=[f"XML 파싱 실패 ({source}): {e}"])
    elif isinstance(source, (str, bytes)):
        try:
            root = ET.fromstring(source)
        except Exception as e:
            return ValidationResult(is_valid=False, errors=[f"XML 문자열 파싱 실패: {e}"])
    else:
        return ValidationResult(is_valid=False, errors=[f"지원하지 않는 입력 타입: {type(source)}"])

    # 1. 루트 태그 확인
    if _strip_ns(root.tag).lower() != "task":
        errors.append(f"루트 요소가 Task가 아닙니다: {_strip_ns(root.tag)}")

    # 2. BootTrigger 확인 (REQ-026: 부팅 시 자동 시작)
    triggers = _find_child(root, "Triggers")
    if triggers is None:
        errors.append("<Triggers> 요소가 누락되었습니다.")
    else:
        boot_trigger = _find_child(triggers, "BootTrigger")
        if boot_trigger is None:
            errors.append("부팅 시 자동 시작을 위한 <BootTrigger>가 누락되었습니다.")
        else:
            enabled_str = _get_child_text(boot_trigger, "Enabled", default="true").lower()
            details["boot_trigger_enabled"] = enabled_str != "false"
            if enabled_str == "false":
                errors.append("<BootTrigger>가 비활성화(Enabled=false)되어 있습니다.")

    # 3. Principals 확인 (SYSTEM 계정 및 HighestAvailable)
    principals = _find_child(root, "Principals")
    if principals is None:
        errors.append("<Principals> 요소가 누락되었습니다.")
    else:
        principal = _find_child(principals, "Principal")
        if principal is None:
            errors.append("<Principal> 요소가 누락되었습니다.")
        else:
            user_id = _get_child_text(principal, "UserId")
            run_level = _get_child_text(principal, "RunLevel")
            details["user_id"] = user_id
            details["run_level"] = run_level
            if not any(ident in user_id.lower() for ident in SYSTEM_ACCOUNT_IDENTIFIERS):
                errors.append(f"로그인 없이 실행하기 위한 SYSTEM 계정이 아닙니다: {user_id}")
            if run_level.lower() not in ("highestavailable", "highest"):
                errors.append(f"RunLevel이 HighestAvailable이 아닙니다: {run_level}")

    # 4. Settings 확인 (재시작 정책, PT0S 무제한 실행)
    settings = _find_child(root, "Settings")
    if settings is None:
        errors.append("<Settings> 요소가 누락되었습니다.")
    else:
        exec_limit = _get_child_text(settings, "ExecutionTimeLimit")
        details["execution_time_limit"] = exec_limit
        if exec_limit.upper() != "PT0S":
            errors.append(f"ExecutionTimeLimit은 PT0S(무제한)이어야 합니다: {exec_limit}")
        if _get_child_text(settings, "DisallowStartIfOnBatteries").lower() == "true":
            errors.append("DisallowStartIfOnBatteries가 true입니다.")
        if _get_child_text(settings, "StopIfGoingOnBatteries").lower() == "true":
            errors.append("StopIfGoingOnBatteries가 true입니다.")

        restart_failure = _find_child(settings, "RestartOnFailure")
        if restart_failure is None:
            errors.append("장애 자동 복구를 위한 <RestartOnFailure> 설정이 누락되었습니다.")
        else:
            interval = _get_child_text(restart_failure, "Interval")
            count = _get_child_text(restart_failure, "Count")
            details["restart_interval"] = interval
            details["restart_count"] = count
            if not interval or not count:
                errors.append("<RestartOnFailure>의 Interval 또는 Count가 누락되었습니다.")

    # 5. Actions 확인
    actions = _find_child(root, "Actions")
    if actions is None:
        errors.append("<Actions> 요소가 누락되었습니다.")
    else:
        exec_elem = _find_child(actions, "Exec")
        if exec_elem is None:
            errors.append("<Exec> 요소가 누락되었습니다.")
        else:
            cmd = _get_child_text(exec_elem, "Command")
            args = _get_child_text(exec_elem, "Arguments")
            details["command"] = cmd
            details["arguments"] = args
            details["working_directory"] = _get_child_text(exec_elem, "WorkingDirectory")
            if not cmd:
                errors.append("<Command>가 비어 있습니다.")
            if "-m server" not in args:
                errors.append(f"<Arguments>에 '-m server' 인자가 누락되었습니다: {args}")

    return ValidationResult(is_valid=(len(errors) == 0), errors=errors, details=details)


def generate_task_xml(
    template_path: Path | str,
    python_path: Path | str,
    project_root: Path | str,
    output_path: Path | str | None = None,
) -> str:
    """템플릿 XML의 플레이스홀더를 치환하여 유효한 작업 정의 XML을 생성한다."""
    tpl = Path(template_path)
    if not tpl.is_file():
        raise FileNotFoundError(f"템플릿 XML을 찾을 수 없습니다: {tpl}")

    content = tpl.read_bytes().decode("utf-8")
    resolved = content.replace("{{PYTHON_PATH}}", str(Path(python_path).resolve())).replace(
        "{{PROJECT_ROOT}}", str(Path(project_root).resolve())
    )

    result = validate_task_xml(resolved)
    if not result.is_valid:
        raise ValueError(f"생성된 XML 검증 실패: {result.errors}")

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(resolved, encoding="utf-8")

    return resolved


def check_server_health(
    host: str = "127.0.0.1",
    port: int = 8080,
    timeout: float = 3.0,
    urlopen_fn: Any = urllib.request.urlopen,
) -> dict[str, Any]:
    """미니PC 서버의 상태 확인 주소(/health) 응답을 확인한다."""
    url = f"http://{host}:{port}/health"
    req = urllib.request.Request(url, headers={"User-Agent": "SmartThingsMiniPC-HealthCheck/1.0"})
    try:
        with urlopen_fn(req, timeout=timeout) as response:
            status_code = response.getcode()
            body = response.read().decode("utf-8")
            data = json.loads(body) if body else {}
            is_healthy = status_code == 200 and data.get("status") == "ok"
            return {"healthy": is_healthy, "status_code": status_code, "url": url, "data": data, "error": None}
    except urllib.error.HTTPError as e:
        return {"healthy": False, "status_code": e.code, "url": url, "data": None, "error": str(e)}
    except Exception as e:
        return {"healthy": False, "status_code": None, "url": url, "data": None, "error": str(e)}


def verify_deployment(
    template_path: Path | None = None,
    check_health: bool = False,
    host: str = "127.0.0.1",
    port: int = 8080,
    urlopen_fn: Any = urllib.request.urlopen,
) -> bool:
    """전체 배포 구성 요소를 일괄 검증한다."""
    base_dir = Path(__file__).parent
    tpl = template_path or (base_dir / "task_definition.xml")

    print("[검사 1/3] 작업 스케줄러 XML 템플릿 검증...")
    res = validate_task_xml(tpl)
    if not res.is_valid:
        print(f"  실패: {res.errors}")
        return False
    print("  통과: BootTrigger, SYSTEM Principal, PT0S, RestartOnFailure 확인 완료")

    print("[검사 2/3] PowerShell 배포 스크립트 존재 확인...")
    reg_script = base_dir / "register_task.ps1"
    unreg_script = base_dir / "unregister_task.ps1"
    if not reg_script.is_file() or not unreg_script.is_file():
        print(f"  실패: 스크립트 파일 누락 (reg={reg_script.exists()}, unreg={unreg_script.exists()})")
        return False
    print("  통과: register_task.ps1 및 unregister_task.ps1 확인 완료")

    if check_health:
        print(f"[검사 3/3] 서버 상태 확인 엔드포인트 검증 (http://{host}:{port}/health)...")
        health = check_server_health(host=host, port=port, urlopen_fn=urlopen_fn)
        if not health["healthy"]:
            print(f"  실패: {health['error']}")
            return False
        print(f"  통과: HTTP 200 OK, 서비스 응답 확인 ({health['data'].get('service')})")
    else:
        print("[검사 3/3] 서버 상태 확인 건너뜀 (--check-health 옵션 없음)")

    return True


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점."""
    parser = argparse.ArgumentParser(description="미니PC 자동 시작 배포 구성 검증")
    parser.add_argument("--check-xml", type=Path, default=None, help="검증할 XML 파일 경로")
    parser.add_argument("--generate-xml", action="store_true", help="템플릿으로부터 인스턴스 XML 생성")
    parser.add_argument("--python", type=str, default=sys.executable, help="Python 실행 파일 경로")
    parser.add_argument("--root", type=str, default=str(Path.cwd()), help="프로젝트 루트 디렉터리")
    parser.add_argument("--out", type=Path, default=None, help="생성된 XML 저장 경로")
    parser.add_argument("--check-health", action="store_true", help="실행 중인 서버 헬스체크 수행")
    parser.add_argument("--host", default="127.0.0.1", help="서버 호스트")
    parser.add_argument("--port", type=int, default=8080, help="서버 포트")
    args = parser.parse_args(argv)

    if args.generate_xml:
        tpl = args.check_xml or (Path(__file__).parent / "task_definition.xml")
        xml_str = generate_task_xml(tpl, args.python, args.root, args.out)
        print("XML 생성 완료:\n" + xml_str)
        return 0

    if args.check_xml:
        res = validate_task_xml(args.check_xml)
        if res.is_valid:
            print("XML 검증 성공:", res.details)
            return 0
        print("XML 검증 실패:", res.errors)
        return 1

    ok = verify_deployment(check_health=args.check_health, host=args.host, port=args.port)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
