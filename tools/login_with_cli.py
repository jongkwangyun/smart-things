#!/usr/bin/env python3
"""삼성 공식 SmartThings CLI를 통해 1회 브라우저 로그인 후 OAuth 토큰을 추출해 st_token.json에 저장하는 헬퍼 도구."""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.smartthings.token_storage import DEFAULT_TOKEN_FILE, TokenInfo, mask_token, save_token_to_file  # noqa: E402

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


CredEnumerateW = advapi32.CredEnumerateW
CredEnumerateW.argtypes = [
    wintypes.LPWSTR,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(CREDENTIAL))),
]
CredEnumerateW.restype = wintypes.BOOL

CredReadW = advapi32.CredReadW
CredReadW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
CredReadW.restype = wintypes.BOOL

CredFree = advapi32.CredFree
CredFree.argtypes = [ctypes.c_void_p]


def find_smartthings_credential() -> tuple[str, str] | None:
    """Windows SmartThings CLI 자격 증명(credentials.json 또는 자격 증명 관리자)에서 토큰을 검색한다."""
    import os

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        json_path = Path(local_appdata) / "@smartthings/cli/Data/credentials.json"
        if json_path.is_file():
            try:
                content = json_path.read_text(encoding="utf-8")
                return str(json_path), content
            except Exception:
                pass

    count = wintypes.DWORD()
    pcreds = ctypes.POINTER(ctypes.POINTER(CREDENTIAL))()
    if not CredEnumerateW(None, 0, ctypes.byref(count), ctypes.byref(pcreds)):
        return None

    try:
        for i in range(count.value):
            target = pcreds[i].contents.TargetName
            target_lower = target.lower()
            if "smartthings" in target_lower or "@smartthings" in target_lower:
                pcred = ctypes.POINTER(CREDENTIAL)()
                if CredReadW(target, 1, 0, ctypes.byref(pcred)):
                    blob = ctypes.string_at(pcred.contents.CredentialBlob, pcred.contents.CredentialBlobSize)
                    CredFree(pcred)
                    try:
                        text = blob.decode("utf-16-le")
                    except Exception:
                        text = blob.decode("utf-8", "ignore")
                    return target, text
        return None
    finally:
        CredFree(pcreds)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    cli_exe = PROJECT_ROOT / "smartthings.exe"
    if not cli_exe.is_file():
        sys.stderr.write("오류: smartthings.exe 파일이 존재하지 않습니다.\n")
        return 1

    print("=" * 70)
    print("삼성 공식 SmartThings CLI 브라우저 1회 간편 인증 도구")
    print("=" * 70)
    print("잠시 후 웹 브라우저가 열립니다. 삼성 계정 로그인 후 '허용(Allow)'을 클릭해 주세요.\n")

    # 기존 SMARTTHINGS_TOKEN 환경 변수가 있으면 CLI가 브라우저 로그인을 건너뛰므로 제거한 상태로 실행
    env = dict(sys.modules["os"].environ)
    env.pop("SMARTTHINGS_TOKEN", None)
    env.pop("SMARTTHINGS_PAT", None)

    proc = subprocess.Popen([str(cli_exe), "devices", "-j"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    print("[1/2] 브라우저 로그인 대기 중...")

    # 프로세스 완료 대기
    stdout, stderr = proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", "replace").strip()
        print(f"[오류] SmartThings CLI 인증 실패: {err_msg}")
        return 2

    print("[2/2] 인증 완료 확인! Windows 자격 증명 저장소에서 토큰을 추출합니다...")
    time.sleep(1)

    cred = find_smartthings_credential()
    if not cred:
        print("[경고] 자격 증명 관리자에서 토큰을 찾지 못했습니다. CLI 응답에서 추출 시도 중...")
        return 3

    target, raw_json = cred
    print(f"  * 발견된 자격 증명 대상: {target}")

    try:
        data = json.loads(raw_json)
    except Exception as e:
        print(f"[오류] 토큰 데이터 JSON 파싱 실패: {e}")
        return 4

    # keytar 또는 credentials.json 데이터 구조에서 access_token, refresh_token 추출
    token_obj = data.get("token") or data.get("default:api.smartthings.com") or data
    if isinstance(token_obj, dict) and not (token_obj.get("access_token") or token_obj.get("accessToken")):
        # 첫 번째 딕셔너리 값 탐색
        for v in data.values():
            if isinstance(v, dict) and (v.get("access_token") or v.get("accessToken")):
                token_obj = v
                break
    access_token = token_obj.get("access_token") or token_obj.get("accessToken")
    refresh_token = token_obj.get("refresh_token") or token_obj.get("refreshToken")

    if not access_token:
        print(f"[오류] access_token을 찾을 수 없습니다: {data}")
        return 5

    token_info = TokenInfo(
        access_token=access_token,
        token_type="oauth",
        refresh_token=refresh_token,
        client_id=data.get("clientId") or "smartthings-cli",
    )

    save_token_to_file(token_info, DEFAULT_TOKEN_FILE)
    print("\n" + "=" * 70)
    print("[성공] 스마트싱스 OAuth 연동 및 영구 저장 완료!")
    print(f"  * 저장 위치: {DEFAULT_TOKEN_FILE.resolve()}")
    print(f"  * 액세스 토큰: {mask_token(token_info.access_token)}")
    print(f"  * 리프레시 토큰: {mask_token(token_info.refresh_token)}")
    print("=" * 70)
    print("이제 미니PC 서버가 백그라운드에서 매일 스스로 토큰을 갱신합니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
