"""집 밖 HTTPS 접속 및 TLS 인증서 검증 스크립트.

D-07, A-06 및 REQ-023 요구사항을 위해 도메인의 TLS 인증서 상태와
/health 엔드포인트 응답을 HTTPS로 검증합니다.
"""

from __future__ import annotations

import argparse
import datetime
import json
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CertificateInfo:
    """TLS 인증서 진단 정보."""

    subject: str = ""
    issuer: str = ""
    san: list[str] = field(default_factory=list)
    not_before: str = ""
    not_after: str = ""
    days_remaining: int = 0
    is_self_signed: bool = False
    warning: str | None = None


@dataclass
class ExternalAccessResult:
    """외부 접속 검증 결과."""

    host: str
    port: int
    https_url: str
    tls_handshake: bool = False
    cert_valid: bool = False
    health_ok: bool = False
    status_code: int | None = None
    latency_ms: float = 0.0
    certificate: CertificateInfo | None = None
    service_payload: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.tls_handshake and self.health_ok and not self.errors


def parse_x509_tuple(elements: tuple[Any, ...]) -> dict[str, str]:
    """getpeercert()가 반환하는 RDN 튜플 목록을 딕셔너리로 변환한다."""
    result: dict[str, str] = {}
    for rdn in elements:
        for item in rdn:
            if isinstance(item, tuple) and len(item) == 2:
                result[str(item[0])] = str(item[1])
    return result


def extract_certificate_info(cert_dict: dict[str, Any]) -> CertificateInfo:
    """getpeercert() 딕셔너리에서 핵심 인증서 정보를 추출한다."""
    subject_map = parse_x509_tuple(cert_dict.get("subject", ()))
    issuer_map = parse_x509_tuple(cert_dict.get("issuer", ()))

    subject_cn = subject_map.get("commonName", "")
    issuer_cn = issuer_map.get("commonName", "")
    issuer_org = issuer_map.get("organizationName", "")

    # Subject Alternative Names (SAN)
    sans: list[str] = []
    for kind, val in cert_dict.get("subjectAltName", ()):
        sans.append(f"{kind}:{val}")

    # 유효기간 파싱
    not_before = str(cert_dict.get("notBefore", ""))
    not_after = str(cert_dict.get("notAfter", ""))
    days_remaining = 0
    warnings: list[str] = []
    if not_after:
        try:
            exp_date = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.UTC)
            days_remaining = (exp_date - datetime.datetime.now(datetime.UTC)).days
            if days_remaining < 7:
                warnings.append(f"인증서 만료가 임박했습니다 ({days_remaining}일 남음)")
        except ValueError:
            pass

    is_self_signed = subject_cn == issuer_cn and (not issuer_org or issuer_org == subject_map.get("organizationName"))
    if is_self_signed:
        warnings.append("자체 서명(Self-Signed) 인증서입니다. 모바일 기기에서 인증서 경고가 발생할 수 있습니다.")

    warning = "; ".join(warnings) if warnings else None
    issuer_display = issuer_org or issuer_cn or "Unknown Issuer"

    return CertificateInfo(
        subject=subject_cn or "Unknown Subject",
        issuer=issuer_display,
        san=sans,
        not_before=not_before,
        not_after=not_after,
        days_remaining=days_remaining,
        is_self_signed=is_self_signed,
        warning=warning,
    )


def check_tls_connection(
    host: str,
    port: int = 443,
    timeout: float = 5.0,
    insecure: bool = False,
    ssl_context: ssl.SSLContext | None = None,
    connect_fn: Any = None,
) -> tuple[bool, CertificateInfo | None, str | None]:
    """호스트와 TLS 핸드셰이크를 수행하고 인증서 정보를 검증한다."""
    if ssl_context is None:
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        else:
            ctx = ssl.create_default_context()
    else:
        ctx = ssl_context

    try:
        if connect_fn is not None:
            wrapped = connect_fn(host, port, ctx, timeout)
            cert = wrapped.getpeercert()
            cert_info = extract_certificate_info(cert) if cert else None
            return True, cert_info, None

        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
                cert_info = extract_certificate_info(cert) if cert else None
                return True, cert_info, None
    except ssl.SSLCertVerificationError as e:
        return False, None, f"TLS 인증서 검증 실패 (모바일 경고 유발): {e.verify_message}"
    except TimeoutError:
        return False, None, f"연결 시간 초과 ({timeout}초)"
    except ConnectionRefusedError:
        return False, None, f"포트 {port} 연결 거부됨 (포트포워딩 미설정 또는 서버 미실행)"
    except Exception as e:
        return False, None, f"TLS 연결 오류: {e}"


def check_external_access(
    host: str,
    port: int = 443,
    path: str = "/health",
    timeout: float = 5.0,
    insecure: bool = False,
    ssl_context: ssl.SSLContext | None = None,
    connect_fn: Any = None,
    urlopen_fn: Any = urllib.request.urlopen,
) -> ExternalAccessResult:
    """도메인의 TLS 상태와 HTTPS /health 응답을 종합 점검한다."""
    url = f"https://{host}:{port}{path}" if port != 443 else f"https://{host}{path}"
    res = ExternalAccessResult(host=host, port=port, https_url=url)

    # 1. TLS 연결 및 인증서 검증
    tls_ok, cert_info, tls_err = check_tls_connection(
        host=host,
        port=port,
        timeout=timeout,
        insecure=insecure,
        ssl_context=ssl_context,
        connect_fn=connect_fn,
    )
    res.tls_handshake = tls_ok
    res.certificate = cert_info
    if not tls_ok and tls_err:
        res.errors.append(tls_err)
    if cert_info and not cert_info.is_self_signed:
        res.cert_valid = True

    # 2. HTTPS /health 엔드포인트 프로브
    if ssl_context is None:
        if insecure:
            req_ctx = ssl.create_default_context()
            req_ctx.check_hostname = False
            req_ctx.verify_mode = ssl.CERT_NONE
        else:
            req_ctx = ssl.create_default_context()
    else:
        req_ctx = ssl_context

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SmartThingsExternalCheck/1.0", "Accept": "application/json"},
    )

    t0 = time.perf_counter()
    try:
        with urlopen_fn(req, context=req_ctx, timeout=timeout) as resp:
            res.latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            res.status_code = resp.getcode()
            body_bytes = resp.read()
            data = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            res.service_payload = data
            if res.status_code == 200 and data.get("status") == "ok":
                res.health_ok = True
            else:
                res.errors.append(f"예상치 못한 응답: HTTP {res.status_code}, data={data}")
    except urllib.error.HTTPError as e:
        res.latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        res.status_code = e.code
        res.errors.append(f"HTTP 오류 {e.code}: {e.reason}")
    except Exception as e:
        res.latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
        res.errors.append(f"HTTPS 요청 실패: {e}")

    return res


def print_report(result: ExternalAccessResult) -> None:
    """사람이 읽기 쉬운 한국어 진단 보고서를 출력한다."""
    print("=" * 60)
    print(f" 스마트싱스 미니PC HTTPS 외부 접속 점검: {result.host}:{result.port}")
    print("=" * 60)
    print(f"대상 URL: {result.https_url}")
    print(f"TLS 핸드셰이크: {'성공 (OK)' if result.tls_handshake else '실패 (FAIL)'}")

    if result.certificate:
        c = result.certificate
        print(f"인증서 주체 (Subject): {c.subject}")
        print(f"발급 기관 (Issuer):   {c.issuer}")
        print(f"유효 기간 (만료일):   {c.not_after} (잔여: {c.days_remaining}일)")
        if c.san:
            print(f"대체 이름 (SAN):      {', '.join(c.san)}")
        if c.warning:
            print(f"⚠️  주의사항:         {c.warning}")

    print(f"상태 코드 (HTTP):     {result.status_code or '연결 불가'}")
    print(f"응답 지연시간:        {result.latency_ms} ms")

    if result.service_payload:
        svc = result.service_payload.get("service", "unknown")
        kst = result.service_payload.get("time_kst", "")
        print(f"서비스 식별자:        {svc} (KST: {kst})")

    if result.errors:
        print("\n발생 오류:")
        for err in result.errors:
            print(f" - {err}")

    print("-" * 60)
    if result.is_success:
        print("✅ 검증 통과: 모바일 데이터 환경에서 인증서 경고 없이 접속 가능합니다.")
    else:
        print("❌ 검증 실패: 외부 접속 설정(DDNS, 포트포워딩, Caddy, 방화벽)을 확인하세요.")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="집 밖 HTTPS 접속 및 TLS 인증서 검증 도구")
    parser.add_argument("--domain", "-d", default="localhost", help="접속할 DDNS 도메인 또는 호스트")
    parser.add_argument("--port", "-p", type=int, default=443, help="HTTPS 포트 (기본값: 443)")
    parser.add_argument("--path", default="/health", help="상태 확인 경로 (기본값: /health)")
    parser.add_argument("--timeout", type=float, default=5.0, help="연결 제한 시간(초)")
    parser.add_argument("--insecure", "-k", action="store_true", help="자체 서명 인증서 검증 완화 (테스트용)")
    parser.add_argument("--json", action="store_true", help="JSON 형식 출력")
    args = parser.parse_args()

    result = check_external_access(
        host=args.domain,
        port=args.port,
        path=args.path,
        timeout=args.timeout,
        insecure=args.insecure,
    )

    if args.json:
        payload = asdict(result)
        payload["is_success"] = result.is_success
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_report(result)

    sys.exit(0 if result.is_success else 1)


if __name__ == "__main__":
    main()
