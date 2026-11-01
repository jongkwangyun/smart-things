<#
.SYNOPSIS
    미니PC 자동 시작 작업 스케줄러 작업을 해제(삭제)합니다.

.DESCRIPTION
    Windows 작업 스케줄러(Task Scheduler)에서 자동 시작 작업을 삭제합니다.

.PARAMETER TaskName
    삭제할 작업 스케줄러 작업 이름 (기본값: SmartThingsMiniPCServer)

.PARAMETER DryRun
    실제 삭제를 수행하지 않고 대상 명령만 출력
#>
[CmdletBinding()]
param(
    [string]$TaskName = "SmartThingsMiniPCServer",
    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

Write-Host "=================================================="
Write-Host "스마트싱스 미니PC 서버 자동 시작 작업 등록 해제"
Write-Host "=================================================="
Write-Host "TaskName : $TaskName"

if ($DryRun) {
    Write-Host "[DryRun] 실제 작업 삭제는 수행되지 않았습니다."
    Write-Host "[DryRun] 대상 명령: schtasks.exe /Delete /TN `"$TaskName`" /F"
    exit 0
}

if (-not (Test-IsAdmin)) {
    Write-Error "작업 스케줄러 등록 해제를 위해서는 관리자 권한이 필요합니다. 관리자 권한으로 PowerShell을 실행하여 다시 시도하십시오. (검증은 -DryRun 사용)"
    exit 1
}

$proc = Start-Process -FilePath "schtasks.exe" -ArgumentList @("/Delete", "/TN", $TaskName, "/F") -Wait -NoNewWindow -PassThru
if ($proc.ExitCode -ne 0) {
    Write-Error "schtasks.exe 삭제 실패 (ExitCode: $($proc.ExitCode))"
    exit $proc.ExitCode
}

Write-Host "작업 스케줄러 작업이 정상적으로 삭제되었습니다: $TaskName"
exit 0
