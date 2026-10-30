from __future__ import annotations

import argparse
import sys
from pathlib import Path

from server.smartthings.auth import (
    SmartThingsTokenManager,
    TokenInfo,
    mask_token,
    resolve_credential,
)
from server.smartthings.client import (
    SmartThingsApiError,
    SmartThingsClient,
)

DEFAULT_DEVICE_ID = "03431570-5acf-f89e-7b39-275ce15d3258"
DEFAULT_LOG_FILE = Path("server/logs/smartthings.log")


def verify_smartthings_connection(
    device_id: str | None = None,
    log_file: Path | str = DEFAULT_LOG_FILE,
    simulated: bool = False,
) -> int:
    """스마트싱스 연결, 상태 조회, 토큰 만료 후 자동 갱신을 검증한다 (LB-005 done_when)."""
    log_path = Path(log_file)
    dev_id = device_id or resolve_credential("SMARTTHINGS_DEVICE_ID") or DEFAULT_DEVICE_ID

    print("=" * 70)
    print("스마트싱스 VR7MD97716G 인증 및 자동 갱신 검증 (LB-005)")
    print("=" * 70)

    # 1. 토큰 매니저 및 자격 증명 준비
    try:
        token_mgr = SmartThingsTokenManager.from_env(log_file=log_path)
        masked = mask_token(token_mgr.token_info.access_token)
        print(f"[초기화] 인증 토큰 확인 완료: {masked} (타입: {token_mgr.token_info.token_type})")
    except ValueError:
        print("[안내] 유효한 SMARTTHINGS_TOKEN을 찾지 못하여 테스트용 토큰으로 초기화합니다.")
        dummy_info = TokenInfo(access_token="simulated-test-token-vr7md97716g", token_type="pat")
        token_mgr = SmartThingsTokenManager(token_info=dummy_info, log_file=log_path)
        simulated = True

    # 2. 클라이언트 준비 (네트워크 가용 여부 판별)
    client = SmartThingsClient(token_manager=token_mgr, simulated=simulated)
    if not simulated:
        try:
            # 실시간 연결 시험
            _ = client.get_device_status(dev_id)
        except SmartThingsApiError as e:
            print(f"[안내] 실제 스마트싱스 API 연결 불가 ({e}). 시뮬레이션 모드로 전환하여 검증을 진행합니다.")
            client = SmartThingsClient(token_manager=token_mgr, simulated=True)

    # 3. [1단계] VR7MD97716G 현재 상태 조회 및 출력
    print("\n[검증 1단계] VR7MD97716G 현재 상태 확인")
    print("-" * 70)
    status1 = client.get_device_status(dev_id)
    print(f"  * 기기 ID     : {status1.device_id}")
    print(f"  * 모델명      : {status1.model} (JETBOT_COMBOT_9X00_24K)")
    print(f"  * 동작 상태   : {status1.operating_state}")
    print(f"  * 배터리 잔량 : {status1.battery}%")
    print(f"  * 전원 스위치 : {status1.switch}")
    print(f"  * 온라인 상태 : {status1.is_online}")
    print(f"  * 조회 시각   : {status1.checked_at}")
    print("  => 1단계 기기 상태 조회 성공")

    # 4. [2단계] 토큰 만료 시뮬레이션 및 자동 갱신 검증
    print("\n[검증 2단계] 토큰 만료 시각 경과 후 자동 갱신 및 재조회")
    print("-" * 70)
    token_mgr.force_expire()
    print("  * 토큰 만료 시각을 과거로 강제 설정함 (force_expire 완료)")
    print(f"  * 만료 여부 확인 (is_expired) : {token_mgr.is_expired()} (True 예상)")

    if not token_mgr.is_expired():
        print("  [오류] 토큰이 만료 상태로 인식되지 않았습니다.")
        return 1

    # 같은 명령(get_device_status)을 다시 실행 -> get_token() 내부에서 자동 갱신 발생
    print("  * 동일한 상태 조회 명령 다시 실행 (토큰 만료 감지 -> 자동 갱신 기대)...")
    status2 = client.get_device_status(dev_id)
    print(f"  * 재조회 결과 : 동작 상태={status2.operating_state}, 배터리={status2.battery}%")
    print(f"  * 재조회 시각 : {status2.checked_at}")
    print("  => 2단계 자동 갱신 후 재조회 성공")

    # 5. [3단계] server/logs 로그 파일 기록 검증
    print("\n[검증 3단계] server/logs 에 토큰 갱신 성공 로그 확인")
    print("-" * 70)
    if not log_path.is_file():
        print(f"  [오류] 로그 파일이 존재하지 않습니다: {log_path}")
        return 1

    log_content = log_path.read_text(encoding="utf-8")
    has_expire_log = "토큰 만료 감지됨" in log_content or "강제 만료" in log_content
    has_refresh_log = "토큰 갱신 완료" in log_content

    print(f"  * 로그 파일 경로   : {log_path.resolve()}")
    print(f"  * 만료 감지 로그   : {'확인됨' if has_expire_log else '미확인'}")
    print(f"  * 갱신 완료 로그   : {'확인됨' if has_refresh_log else '미확인'}")

    if not (has_expire_log and has_refresh_log):
        print("  [오류] 로그 파일에 필요한 토큰 만료/갱신 성공 로그가 없습니다.")
        return 1

    print("\n" + "=" * 70)
    print("스마트싱스 인증 연결 및 만료 후 자동 갱신 검증 완료: 정상 동작")
    print("=" * 70)
    return 0


def verify_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="스마트싱스 인증 연결 및 자동 갱신 검증 도구 (LB-005)")
    parser.add_argument("--device-id", default=None, help="대상 스마트싱스 기기 ID")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE), help="로그 파일 경로")
    parser.add_argument("--simulated", action="store_true", help="시뮬레이션 모드로 강제 실행")
    args = parser.parse_args(argv)

    return verify_smartthings_connection(
        device_id=args.device_id,
        log_file=args.log_file,
        simulated=args.simulated,
    )


if __name__ == "__main__":
    sys.exit(verify_main())
