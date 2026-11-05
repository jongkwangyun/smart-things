#!/usr/bin/env python3
"""스마트싱스 OAuth 2.0 초기 인증 코드(authorization_code) 교환 및 토큰 저장 도구.

사용법:
    py tools/setup_smartthings_oauth.py --client-id <ID> --client-secret <SECRET> --code <CODE> --redirect-uri <URI>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가하여 server 모듈 임포트 가능하도록 설정
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.smartthings.token_storage import (  # noqa: E402
    DEFAULT_TOKEN_FILE,
    TokenInfo,
    mask_token,
    save_token_to_file,
)
from server.time_utils import format_kst, now_kst  # noqa: E402


def exchange_oauth_code(
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    token_url: str = "https://api.smartthings.com/oauth/token",
) -> TokenInfo:
    """OAuth authorization_code를 access_token 및 refresh_token과 교환한다."""
    payload = {
        "grant_type": "authorization_code",
        "client_id": client_id.strip(),
        "client_secret": client_secret.strip(),
        "code": code.strip(),
        "redirect_uri": redirect_uri.strip(),
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(token_url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"스마트싱스 OAuth 교환 실패 (HTTP {e.code}): {err_body}") from e
    except Exception as e:
        raise RuntimeError(f"스마트싱스 OAuth 네트워크 요청 실패: {e}") from e

    access_token = body.get("access_token")
    if not access_token:
        raise ValueError(f"응답에 access_token이 누락되었습니다: {body}")

    refresh_token = body.get("refresh_token")
    expires_in = int(body.get("expires_in", 86400))
    expires_at = now_kst() + timedelta(seconds=expires_in)

    return TokenInfo(
        access_token=access_token,
        token_type="oauth",
        refresh_token=refresh_token,
        expires_at=expires_at,
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        token_url=token_url,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="스마트싱스 OAuth 2.0 첫 토큰 발급 및 영구 저장 헬퍼")
    parser.add_argument("--client-id", dest="client_id", help="스마트싱스 OAuth Client ID")
    parser.add_argument("--client-secret", dest="client_secret", help="스마트싱스 OAuth Client Secret")
    parser.add_argument("--code", dest="code", help="OAuth 인증 후 수신한 authorization_code")
    parser.add_argument(
        "--redirect-uri",
        dest="redirect_uri",
        default="https://httpbin.org/get",
        help="스마트싱스 개발자 콘솔에 등록된 Redirect URI (기본값: https://httpbin.org/get)",
    )
    parser.add_argument(
        "--token-file",
        dest="token_file",
        default=str(DEFAULT_TOKEN_FILE),
        help=f"토큰을 저장할 파일 경로 (기본값: {DEFAULT_TOKEN_FILE})",
    )
    parser.add_argument(
        "--token-url",
        dest="token_url",
        default="https://api.smartthings.com/oauth/token",
        help="스마트싱스 OAuth 토큰 엔드포인트",
    )

    args = parser.parse_args()

    client_id = args.client_id
    client_secret = args.client_secret
    code = args.code
    redirect_uri = args.redirect_uri

    is_interactive = sys.stdin.isatty()
    if not client_id and is_interactive:
        client_id = input("SmartThings Client ID: ").strip()
    if not client_secret and is_interactive:
        client_secret = input("SmartThings Client Secret: ").strip()
    if not code and is_interactive:
        code = input("Authorization Code: ").strip()

    if not client_id or not client_secret or not code:
        sys.stderr.write("오류: client_id, client_secret, code는 필수 항목입니다.\n")
        parser.print_help()
        return 1

    try:
        print("[OAuth] 스마트싱스 토큰 교환을 진행합니다...")
        token_info = exchange_oauth_code(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
            token_url=args.token_url,
        )

        target_file = Path(args.token_file)
        save_token_to_file(token_info, target_file)

        print("[OAuth] 토큰 발급 및 영구 파일 저장 완료!")
        print(f"  * 저장 위치: {target_file.resolve()}")
        print(f"  * 액세스 토큰: {mask_token(token_info.access_token)}")
        print(f"  * 리프레시 토큰: {mask_token(token_info.refresh_token)}")
        print(f"  * 만료 예정 일시(KST): {format_kst(token_info.expires_at) if token_info.expires_at else '없음'}")
        return 0
    except Exception as e:
        sys.stderr.write(f"[OAuth 오류] {e}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
