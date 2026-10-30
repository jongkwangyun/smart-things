<#
.SYNOPSIS
    스마트싱스 미니PC 외부 접속(HTTPS + DDNS + 포트포워딩) 설정 및 진단 스크립트.

.DESCRIPTION
    D-07 (포트포워딩 + HTTPS), A-06 (공유기 DDNS), REQ-023 (집 밖에서 모바일 데이터로 사용)을 위한
    공유기 DDNS 확인, 윈도우 방화벽 인바운드 규칙 설정, 역방향 프록시(Caddy) 구성을 안내하고 진단합니다.

.PARAMETER Domain
    공유기 DDNS 도메인 이름 (예: myhome.iptime.org).

.PARAMETER BackendPort
    미니PC 로컬 서버 포트 (기본값: 8080).

.PARAMETER CheckOnly
    설정 변경 없이 진단만 수행합니다.

.PARAMETER ConfigureFirewall
    Windows Defender 방화벽에 80(HTTP), 443(HTTPS) 인바운드 허용 규칙을 등록합니다. (관리자 권한 필요)
#>

[CmdletBinding()]
param(
    [string]$Domain = "",
    [int]$BackendPort = 8080,
    [switch]$CheckOnly,
    [switch]$ConfigureFirewall
)

$ErrorActionPreference = "Continue"

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host "  스마트싱스 미니PC HTTPS 외부 접속 구성 및 진단 (LB-008)  " -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. 미니PC 로컬 IP 및 공인 IP 확인
Write-Host "`n[1/5] 네트워크 IP 정보 확인..." -ForegroundColor Yellow
$localIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -ExpandProperty IPAddress

$primaryLocalIp = if ($localIps) { $localIps[0] } else { "127.0.0.1" }
Write-Host "  - 미니PC 내부 IP (공유기 할당): $primaryLocalIp" -ForegroundColor Green

$publicIp = ""
try {
    $publicIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 3 -ErrorAction Stop).Trim()
    Write-Host "  - 현재 공인 IP (외부 확인):   $publicIp" -ForegroundColor Green
} catch {
    Write-Host "  ! 공인 IP 자동 조회 건너뜀 (외부 확인 서비스 연결 불가)" -ForegroundColor DarkGray
}

# 2. DDNS 도메인 DNS 해석 확인 (A-06)
Write-Host "`n[2/5] DDNS 도메인 해석 확인 (A-06)..." -ForegroundColor Yellow
if (-not [string]::IsNullOrWhiteSpace($Domain)) {
    try {
        $dnsResult = [System.Net.Dns]::GetHostAddresses($Domain) |
            Where-Object { $_.AddressFamily -eq 'InterNetwork' } |
            Select-Object -ExpandProperty IPAddressToString

        if ($dnsResult) {
            Write-Host "  - DDNS 도메인: $Domain -> $dnsResult" -ForegroundColor Green
            if ($publicIp -and ($dnsResult -contains $publicIp)) {
                Write-Host "  - 일치 확인: DDNS 도메인이 현재 공인 IP와 정확히 일치합니다." -ForegroundColor Green
            } elseif ($publicIp) {
                Write-Host "  ! 불일치 주의: DDNS IP($dnsResult)와 공인 IP($publicIp)가 다릅니다. 공유기 DDNS 갱신 상태를 확인하세요." -ForegroundColor Yellow
            }
        } else {
            Write-Host "  ! 경고: 도메인($Domain)의 IPv4 주소를 찾을 수 없습니다." -ForegroundColor Red
        }
    } catch {
        Write-Host "  ! DNS 해석 실패 ($Domain): $($_.Exception.Message)" -ForegroundColor Red
    }
} else {
    Write-Host "  - 도메인 미지정: -Domain <ddns도메인> 옵션으로 실행하면 DDNS 일치 여부를 검증합니다." -ForegroundColor DarkGray
}

# 3. 윈도우 방화벽 인바운드 포트 검사 및 구성
Write-Host "`n[3/5] 윈도우 방화벽 인바운드 규칙 점검..." -ForegroundColor Yellow
$firewallPorts = @(80, 443, $BackendPort)
foreach ($p in $firewallPorts) {
    $rules = Get-NetFirewallPortFilter -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -eq "$p" -and $_.Protocol -eq 'TCP' }
    if ($rules) {
        Write-Host "  - TCP 포트 $p : 방화벽 규칙 존재 확인" -ForegroundColor Green
    } else {
        Write-Host "  - TCP 포트 $p : 방화벽 인바운드 규칙 없음" -ForegroundColor Yellow
    }
}

if ($ConfigureFirewall) {
    Write-Host "  -> 방화벽 인바운드 규칙 등록 시도 (포트 80, 443, $BackendPort)..." -ForegroundColor Cyan
    try {
        New-NetFirewallRule -DisplayName "SmartThings-HTTP-80" -Direction Inbound -LocalPort 80 -Protocol TCP -Action Allow -ErrorAction Stop | Out-Null
        New-NetFirewallRule -DisplayName "SmartThings-HTTPS-443" -Direction Inbound -LocalPort 443 -Protocol TCP -Action Allow -ErrorAction Stop | Out-Null
        New-NetFirewallRule -DisplayName "SmartThings-Backend-$BackendPort" -Direction Inbound -LocalPort $BackendPort -Protocol TCP -Action Allow -ErrorAction Stop | Out-Null
        Write-Host "  - 방화벽 규칙 등록 완료 (80, 443, $BackendPort)" -ForegroundColor Green
    } catch {
        Write-Host "  ! 방화벽 규칙 등록 실패 (관리자 권한 필요): $($_.Exception.Message)" -ForegroundColor Red
    }
}

# 4. 공유기 포트포워딩 설정 안내 (D-07)
Write-Host "`n[4/5] 공유기 포트포워딩 설정 안내 (D-07)..." -ForegroundColor Yellow
Write-Host "  공유기 관리자 웹페이지(예: 192.168.0.1 또는 192.168.1.1)에 접속하여"
Write-Host "  [포트포워딩 설정] 메뉴에서 아래 규칙을 추가하십시오:"
Write-Host "  ------------------------------------------------------------------"
Write-Host "  규칙 1 (HTTPS): 외부 포트 443  -> 내부 IP $primaryLocalIp, 내부 포트 443  (TCP)" -ForegroundColor Cyan
Write-Host "  규칙 2 (ACME):  외부 포트 80   -> 내부 IP $primaryLocalIp, 내부 포트 80   (TCP)" -ForegroundColor Cyan
Write-Host "  ------------------------------------------------------------------"
Write-Host "  * 포트 80은 Let's Encrypt / ZeroSSL 공인 인증서 자동 발급(HTTP-01 챌린지)에 필요합니다."
Write-Host "  * 모바일 브라우저(갤럭시 S21+, 폴드8)에서 인증서 경고 없이 접속하려면 공인 CA 인증서가 필수입니다."

# 5. 실행 방식 및 대안 안내 (must: VPN 전환 기준)
Write-Host "`n[5/5] HTTPS 역방향 프록시(Caddy) 실행 및 VPN 대안 안내..." -ForegroundColor Yellow
Write-Host "  [권장: Caddy 역방향 프록시 실행]"
Write-Host "  1) Caddy 다운로드 (https://caddyserver.com 또는 winget install CaddyServer.Caddy)"
Write-Host "  2) 환경변수 설정: `$env:SMART_THINGS_DOMAIN = '$Domain'"
Write-Host "  3) 프록시 시작: caddy run --config server/deploy/Caddyfile"
Write-Host "  4) 외부 접속 테스트: .venv\Scripts\python.exe server/deploy/check_external_access.py --domain $Domain"
Write-Host ""
Write-Host "  [ISP 포트 차단 또는 포트포워딩 불가 시 VPN 대안 (must 규칙)]"
Write-Host "  - 일부 통신사(ISP)에서 80/443 포트를 차단하거나 공유기 포트포워딩이 지원되지 않는 경우:"
Write-Host "    -> Tailscale 또는 WireGuard VPN으로 전환하고 memo에 기록합니다."
Write-Host "    -> 미니PC에 Tailscale 설치 후 두 대의 휴대폰을 같은 Tailnet에 등록하면"
Write-Host "       별도 포트포워딩 없이 Tailscale MagicDNS(HTTPS)로 안전하게 연결됩니다."
Write-Host "==========================================================" -ForegroundColor Cyan
