# ensure_server.ps1 - SmartThings mini-PC server watchdog (run every 30 min by Task Scheduler).
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 reads BOM-less UTF-8 as ANSI and breaks.
#
# 1. Checks /health 3 times (5s limit each). All 200 -> healthy, exit.
# 2. Otherwise (not running, or hung: accepts connections but never answers) kills the python
#    processes holding the port, then starts the server again and verifies it.
# All external calls have time limits, so this script itself cannot hang.
param(
    [int]$Port = 0
)

$ErrorActionPreference = "Continue"
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "server\logs"
$log = Join-Path $logDir "ensure_server.log"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

function Write-Log([string]$msg) {
    $kst = [DateTimeOffset]::UtcNow.ToOffset([TimeSpan]::FromHours(9)).ToString("yyyy-MM-dd HH:mm:ss")
    Add-Content -Path $log -Value "[$kst KST] $msg"
}

if ($Port -le 0) {
    $Port = 8080
    $envFile = Join-Path $root ".env"
    if (Test-Path $envFile) {
        foreach ($l in Get-Content $envFile) {
            if ($l -match '^\s*SMART_THINGS_PORT\s*=\s*(\d+)\s*$') { $Port = [int]$Matches[1] }
        }
    }
}

function Test-Health {
    $code = & curl.exe -s -o NUL -w "%{http_code}" --connect-timeout 3 --max-time 5 "http://127.0.0.1:$Port/health" 2>$null
    return ($code -eq "200")
}

$ok = $true
for ($i = 0; $i -lt 3; $i++) {
    if (-not (Test-Health)) { $ok = $false; break }
    Start-Sleep -Milliseconds 500
}

if ($ok) {
    Write-Log "OK: server healthy (port $Port)"
    exit 0
}

Write-Log "UNHEALTHY (port $Port): cleaning up and restarting"

$pids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique)
foreach ($procId in $pids) {
    $parentId = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue).ParentProcessId
    foreach ($target in @($procId, $parentId)) {
        if ($target -and $target -ne $PID) {
            $p = Get-Process -Id $target -ErrorAction SilentlyContinue
            if ($p -and $p.ProcessName -match 'python') {
                try {
                    Stop-Process -Id $target -Force -ErrorAction Stop
                    Write-Log "killed PID $target"
                } catch {
                    Write-Log "kill failed PID ${target}: $($_.Exception.Message)"
                }
            }
        }
    }
}
Start-Sleep -Seconds 2

Start-Process -FilePath $python -ArgumentList @("-m", "server") -WorkingDirectory $root -WindowStyle Hidden
Write-Log "start command issued"

$up = $false
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 2
    if (Test-Health) { $up = $true; break }
}
if ($up) { Write-Log "verified: server healthy" } else { Write-Log "verify FAILED: no response" }
exit 0
