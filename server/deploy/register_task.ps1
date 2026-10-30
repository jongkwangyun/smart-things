<#
.SYNOPSIS
    미니PC 재부팅 시 스마트싱스 서버를 자동 시작하도록 작업 스케줄러에 등록합니다.

.DESCRIPTION
    부팅 시 사용자 로그인 없이(NT AUTHORITY\SYSTEM) 서버가 시작되도록
    Windows 작업 스케줄러(Task Scheduler) 작업을 생성/갱신합니다.
    - REQ-026 (부팅 시 자동 시작 및 장애 복구)
    - must: 사람의 수동 조작 없이 복구되어야 한다.

.PARAMETER TaskName
    작업 스케줄러에 등록할 작업 이름 (기본값: SmartThingsMiniPCServer)

.PARAMETER ProjectRoot
    프로젝트 루트 디렉터리 경로 (기본값: 저장소 루트 자동 감지)

.PARAMETER PythonExe
    Python 실행 파일 경로 (기본값: .venv\Scripts\python.exe 또는 시스템 python)

.PARAMETER XmlTemplatePath
    작업 정의 템플릿 XML 파일 경로 (기본값: task_definition.xml)

.PARAMETER OutputXmlPath
    치환된 인스턴스 XML 파일 저장 경로 (기본값: task_instance.xml)

.PARAMETER DryRun
    실제 작업을 등록하지 않고 생성될 XML 및 실행 명령만 검증/출력

.PARAMETER Force
    기존 작업이 있으면 덮어씁니다 (기본값: $true)
#>
[CmdletBinding()]
param(
    [string]$TaskName = "SmartThingsMiniPCServer",
    [string]$ProjectRoot = "",
    [string]$PythonExe = "",
    [string]$XmlTemplatePath = "",
    [string]$OutputXmlPath = "",
    [switch]$DryRun,
    [switch]$Force = $true
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# 1. 경로 확인 및 기본값 설정
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path "$scriptDir\..\..").Path
} else {
    $ProjectRoot = (Resolve-Path $ProjectRoot).Path
}

if (-not $PythonExe) {
    $venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        $PythonExe = (Resolve-Path $venvPython).Path
    } else {
        $found = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($found) {
            $PythonExe = $found.Source
        } else {
            $PythonExe = $venvPython
        }
    }
}

if (-not $XmlTemplatePath) {
    $XmlTemplatePath = Join-Path $scriptDir "task_definition.xml"
}

if (-not (Test-Path $XmlTemplatePath)) {
    throw "작업 정의 템플릿 XML을 찾을 수 없습니다: $XmlTemplatePath"
}

if (-not $OutputXmlPath) {
    $OutputXmlPath = Join-Path $scriptDir "task_instance.xml"
}

# 2. 템플릿 치환
$templateContent = [System.IO.File]::ReadAllText($XmlTemplatePath, [System.Text.Encoding]::UTF8)
$resolvedXml = $templateContent.Replace("{{PYTHON_PATH}}", $PythonExe).Replace("{{PROJECT_ROOT}}", $ProjectRoot)

# UTF-16 LE로 인스턴스 XML 파일 저장 (schtasks.exe 및 Register-ScheduledTask 완전 호환)
$utf16 = [System.Text.Encoding]::Unicode
[System.IO.File]::WriteAllText($OutputXmlPath, $resolvedXml, $utf16)

Write-Host "=================================================="
Write-Host "스마트싱스 미니PC 서버 자동 시작 작업 등록"
Write-Host "=================================================="
Write-Host "TaskName       : $TaskName"
Write-Host "ProjectRoot    : $ProjectRoot"
Write-Host "PythonExe      : $PythonExe"
Write-Host "OutputXmlPath  : $OutputXmlPath"

if ($DryRun) {
    Write-Host "[DryRun] 실제 작업 등록은 수행되지 않았습니다."
    Write-Host "[DryRun] 대상 명령: schtasks.exe /Create /TN `"$TaskName`" /XML `"$OutputXmlPath`" $(if ($Force) { '/F' })"
    exit 0
}

# 3. 관리자 권한 확인
if (-not (Test-IsAdmin)) {
    Write-Error "작업 스케줄러 등록을 위해서는 관리자 권한이 필요합니다. 관리자 권한으로 PowerShell을 실행하여 다시 시도하십시오. (검증은 -DryRun 사용)"
    exit 1
}

# 4. 실제 schtasks 등록 실행
$schtasksArgs = @("/Create", "/TN", $TaskName, "/XML", $OutputXmlPath)
if ($Force) {
    $schtasksArgs += "/F"
}

$proc = Start-Process -FilePath "schtasks.exe" -ArgumentList $schtasksArgs -Wait -NoNewWindow -PassThru
if ($proc.ExitCode -ne 0) {
    Write-Error "schtasks.exe 실행 실패 (ExitCode: $($proc.ExitCode))"
    exit $proc.ExitCode
}

Write-Host "작업 스케줄러 등록이 성공적으로 완료되었습니다: $TaskName"
exit 0
