<#
.SYNOPSIS
    30분마다 ensure_server.ps1 을 실행하는 작업 스케줄러 작업을 등록합니다.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "SmartThingsServerWatchdog",
    [int]$IntervalMinutes = 30
)

$ErrorActionPreference = "Stop"
$script = Join-Path $PSScriptRoot "ensure_server.ps1"
$tr = "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""

schtasks.exe /Create /TN $TaskName /TR $tr /SC MINUTE /MO $IntervalMinutes /F
if ($LASTEXITCODE -ne 0) { throw "schtasks 등록 실패 (ExitCode: $LASTEXITCODE)" }
Write-Host "등록 완료: $TaskName (${IntervalMinutes}분마다)"
