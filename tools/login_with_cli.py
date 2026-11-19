#!/usr/bin/env python3
"""삼성 공식 SmartThings CLI를 통해 1회 브라우저 로그인 후 OAuth 토큰을 추출해 st_token.json에 저장하는 헬퍼 도구."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.smartthings.token_storage import (  # noqa: E402
    DEFAULT_TOKEN_FILE,
    find_cli_credentials_file,
    find_smartthings_cli_executable,
    mask_token,
    parse_cli_credentials,
    save_token_to_file,
)
from server.time_utils import format_kst  # noqa: E402


def _find_windows_credential_manager_entry() -> tuple[str, str] | None:
    """Windows 자격 증명 관리자(CredEnumerateW)에서 SmartThings CLI 자격 증명을 검색한다."""
    if sys.platform != "win32":
        return None

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    cred_enumerate = advapi32.CredEnumerateW
    cred_enumerate.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIAL))),
    ]
    cred_enumerate.restype = wintypes.BOOL

    cred_read = advapi32.CredReadW
    cred_read.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    cred_read.restype = wintypes.BOOL

    cred_free = advapi32.CredFree
    cred_free.argtypes = [ctypes.c_void_p]

    count = wintypes.DWORD()
    pcreds = ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
    if not cred_enumerate(None, 0, ctypes.byref(count), ctypes.byref(pcreds)):
        return None

    try:
        for i in range(count.value):
            target = pcreds[i].contents.TargetName
            target_lower = target.lower()
            if "smartthings" in target_lower or "@smartthings" in target_lower:
                pcred = ctypes.POINTER(CREDENTIAL)()
                if cred_read(target, 1, 0, ctypes.byref(pcred)):
                    blob = ctypes.string_at(pcred.contents.CredentialBlob, pcred.contents.CredentialBlobSize)
                    cred_free(pcred)
                    try:
                        text = blob.decode("utf-16-le")
                    except Exception:
                        text = blob.decode("utf-8", "ignore")
                    return target, text
        return None
    finally:
        cred_free(pcreds)


def find_smartthings_credential() -> tuple[str, str] | None:
    """Windows/Linux SmartThings CLI 자격 증명(credentials.json 또는 Windows 자격 증명 관리자)에서 토큰을 검색한다."""
    cred_file = find_cli_credentials_file()
    if cred_file is not None:
        try:
            content = cred_file.read_text(encoding="utf-8")
            return str(cred_file), content
        except Exception:
            pass

    return _find_windows_credential_manager_entry()


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass

    parser = argparse.ArgumentParser(description="SmartThings CLI OAuth 자격 증명 동기화 및 로그인 도구")
    parser.add_argument(
        "--token-file",
        default=str(PROJECT_ROOT / DEFAULT_TOKEN_FILE),
        help=f"토큰을 저장할 파일 경로 (기본값: {PROJECT_ROOT / DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--sync-only",
        action="store_true",
        help="브라우저 로그인을 실행하지 않고 기존 SmartThings CLI 자격 증명 파일만 동기화",
    )
    args = parser.parse_args(argv)

    if not args.sync_only:
        cli_exe = find_smartthings_cli_executable()
        if cli_exe is None:
            expected_name = "smartthings.exe" if sys.platform == "win32" else "smartthings"
            sys.stderr.write(
                f"오류: SmartThings CLI 실행 파일({expected_name})을 찾을 수 없습니다.\n"
                f"프로젝트 루트({PROJECT_ROOT}) 또는 PATH에 {expected_name} 를 배치한 뒤 다시 실행하거나,\n"
                "이미 credentials.json이 있다면 --sync-only 옵션을, "
                "직접 OAuth 코드를 교환하려면 tools/setup_smartthings_oauth.py 를 사용하세요.\n"
            )
            return 1

        print("=" * 70)
        print("삼성 공식 SmartThings CLI 브라우저 1회 간편 인증 도구")
        print("=" * 70)
        print(f"CLI 실행 파일: {cli_exe}")
        print("잠시 후 웹 브라우저가 열립니다. 삼성 계정 로그인 후 '허용(Allow)'을 클릭해 주세요.\n")

        # 기존 SMARTTHINGS_TOKEN 환경 변수가 있으면 CLI가 브라우저 로그인을 건너뛰므로 제거한 상태로 실행
        env = dict(os.environ)
        env.pop("SMARTTHINGS_TOKEN", None)
        env.pop("SMARTTHINGS_PAT", None)
        env.pop("SMARTTHINGS_ACCESS_TOKEN", None)

        try:
            proc = subprocess.Popen(
                [str(cli_exe), "devices", "-j"],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            sys.stderr.write(f"[오류] SmartThings CLI 실행 실패 ({cli_exe}): {e}\n")
            return 2

        print("[1/2] 브라우저 로그인 대기 중...")
        _, stderr = proc.communicate()

        if proc.returncode != 0:
            err_msg = stderr.decode("utf-8", "replace").strip()
            print(f"[오류] SmartThings CLI 인증 실패: {err_msg}")
            return 2

        print("[2/2] 인증 완료 확인! SmartThings CLI 자격 증명 저장소에서 토큰을 추출합니다...")

    cred = find_smartthings_credential()
    if not cred and not args.sync_only:
        time.sleep(1)
        cred = find_smartthings_credential()
    if not cred:
        print("[경고] SmartThings CLI 자격 증명 저장소에서 토큰을 찾지 못했습니다.")
        return 3

    target, raw_json = cred
    print(f"  * 발견된 자격 증명 대상: {target}")

    token_info = parse_cli_credentials(raw_json)
    if token_info is None:
        print("[오류] SmartThings CLI 자격 증명에서 유효한 access_token을 파싱하지 못했습니다.")
        return 4

    target_file = Path(args.token_file)
    save_token_to_file(token_info, target_file)
    print("\n" + "=" * 70)
    print("[성공] 스마트싱스 OAuth 연동 및 영구 저장 완료!")
    print(f"  * 저장 위치: {target_file.resolve()}")
    print(f"  * 액세스 토큰: {mask_token(token_info.access_token)}")
    print(f"  * 리프레시 토큰: {mask_token(token_info.refresh_token)}")
    if token_info.expires_at:
        print(f"  * 만료 예정 일시(KST): {format_kst(token_info.expires_at)}")
    print("=" * 70)
    print("이제 미니PC 서버가 백그라운드에서 토큰을 자동 관리/갱신합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
