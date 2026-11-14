# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — watchdog_backend (watchdog_backend.ps1)
# Date: 2026-05-21
# ---------------------------------------------------------------------------
# Modernization Backend Watchdog
# Monitors the standalone uvicorn service on -Port (default 18084), restarts it
# if down, and republishes it after backend source changes.
# Runs forever - launched by Start-AutoPublish.ps1 / Start-Modernization.ps1.
[CmdletBinding()]
param(
    [int]$Port = 18084
)

$BackendDir  = Join-Path $PSScriptRoot ''
$PythonExe   = "$BackendDir\.venv\Scripts\python.exe"
$LogDir      = Join-Path $BackendDir 'data\logs'
$LogFile     = Join-Path $LogDir 'modernization_stderr.log'
$StdoutFile  = Join-Path $LogDir 'modernization_stdout.log'
$CheckSecs   = 30
$FrontendDir = Join-Path $BackendDir 'frontend'
$FrontendSyncScript = Join-Path $BackendDir 'Sync-ProductionBuild.ps1'
$FrontendSyncPidFile = Join-Path $BackendDir 'data\frontend-sync.pid'
$WatchdogPidFile = Join-Path $BackendDir 'data\backend-watchdog.pid'
$WatchdogPortFile = Join-Path $BackendDir 'data\backend-watchdog.port'
$RuntimePidFile = Join-Path $BackendDir 'data\modernization.pid'
$EnvFile = Join-Path $BackendDir '.env'
$BackendSourceDirs = @(
    (Join-Path $BackendDir 'api'),
    (Join-Path $BackendDir 'services')
)
$SourceChangeDeferredForActiveJob = $false
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Set-Content -LiteralPath $WatchdogPidFile -Value $PID
Set-Content -LiteralPath $WatchdogPortFile -Value $Port
$ForeignOwnerLogged = $false

$BackendEnvironment = @{}
if (Test-Path -LiteralPath $EnvFile) {
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*([^#][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $BackendEnvironment[$Matches[1]] = $Matches[2]
        }
    }
}

# Function: Ensure-FrontendProductionSync
function Ensure-FrontendProductionSync {
    if (-not (Test-Path -LiteralPath $FrontendSyncScript)) { return }
    $syncRunning = $false
    if (Test-Path -LiteralPath $FrontendSyncPidFile) {
        $syncPid = Get-Content -LiteralPath $FrontendSyncPidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($syncPid) { $syncRunning = $null -ne (Get-Process -Id $syncPid -ErrorAction SilentlyContinue) }
    }
    if ($syncRunning) { return }

    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $FrontendSyncPidFile) | Out-Null
    $syncProcess = Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $FrontendSyncScript) `
        -WindowStyle Hidden -PassThru
    Set-Content -LiteralPath $FrontendSyncPidFile -Value $syncProcess.Id
}

# Function: Is-BackendRunning
function Is-BackendRunning {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -ne $conn)
}

# Function: Test-ModernizationProcess
# Only this module's uvicorn server may ever be stopped by the watchdog. Another
# owner of the port (for example Docker Desktop's com.docker.backend publishing
# a container port) must never be killed or "restarted".
function Test-ModernizationProcess([int]$ProcessId) {
    $info = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $info) { return $false }
    return ($info.Name -like 'python*.exe') -and ($info.CommandLine -like '*api.server*')
}

# Function: Get-BackendListenerProcess
function Get-BackendListenerProcess {
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $connection) { return $null }
    if (-not (Test-ModernizationProcess $connection.OwningProcess)) {
        if (-not $script:ForeignOwnerLogged) {
            $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            "[Watchdog $timestamp] Port $Port is owned by PID $($connection.OwningProcess), which is not the Modernization backend; it will not be managed." | Add-Content $LogFile
            $script:ForeignOwnerLogged = $true
        }
        return $null
    }
    $script:ForeignOwnerLogged = $false
    return Get-Process -Id $connection.OwningProcess -ErrorAction SilentlyContinue
}

# Function: Get-LatestBackendSourceWrite
function Get-LatestBackendSourceWrite {
    $latest = [datetime]::MinValue
    foreach ($sourceDir in $BackendSourceDirs) {
        if (-not (Test-Path -LiteralPath $sourceDir)) { continue }
        $candidate = Get-ChildItem -LiteralPath $sourceDir -Recurse -File -Filter '*.py' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        if ($candidate -and $candidate.LastWriteTimeUtc -gt $latest) { $latest = $candidate.LastWriteTimeUtc }
    }
    return $latest
}

# Function: Get-ActiveModernizationJobs
function Get-ActiveModernizationJobs {
    try {
        $resp = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/modernize/jobs/active-count" -Method Get -TimeoutSec 3
        return [int]($resp.active_jobs)
    } catch {
        return $null
    }
}

# Function: Restart-BackendWhenSourceChanged
function Restart-BackendWhenSourceChanged {
    $process = Get-BackendListenerProcess
    if (-not $process) { return }
    $latestSource = Get-LatestBackendSourceWrite
    if ($latestSource -le $process.StartTime.ToUniversalTime()) {
        $script:SourceChangeDeferredForActiveJob = $false
        return
    }

    $activeJobs = Get-ActiveModernizationJobs
    if ($null -eq $activeJobs) {
        if (-not $script:SourceChangeDeferredForActiveJob) {
            $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            "[Watchdog $timestamp] Backend source changed, but active-job state is unavailable; deferring restart safely." | Add-Content $LogFile
            $script:SourceChangeDeferredForActiveJob = $true
        }
        return
    }
    if ($null -ne $activeJobs -and $activeJobs -gt 0) {
        if (-not $script:SourceChangeDeferredForActiveJob) {
            $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
            "[Watchdog $timestamp] Backend source changed, but $activeJobs modernization job(s) are active; deferring restart." | Add-Content $LogFile
            $script:SourceChangeDeferredForActiveJob = $true
        }
        return
    }

    $script:SourceChangeDeferredForActiveJob = $false
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[Watchdog $timestamp] Backend source changed after process start; restarting PID $($process.Id)." | Add-Content $LogFile
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
}

# Function: Start-Backend
function Start-Backend {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName  = $PythonExe
    $psi.Arguments = "-m uvicorn api.server:app --host 127.0.0.1 --port $Port --log-level info"
    $psi.WorkingDirectory       = $BackendDir
    $psi.UseShellExecute        = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError  = $true
    $psi.CreateNoWindow         = $true

    foreach ($entry in $BackendEnvironment.GetEnumerator()) {
        $psi.EnvironmentVariables[$entry.Key] = [string]$entry.Value
    }
    $psi.EnvironmentVariables['AUTH_REQUIRED']       = 'true'
    $psi.EnvironmentVariables['HOST']                = '127.0.0.1'
    $psi.EnvironmentVariables['PORT']                = [string]$Port
    $psi.EnvironmentVariables['CORS_ORIGINS']        = "http://localhost:$Port,http://127.0.0.1:$Port,http://localhost:5175,http://127.0.0.1:5175"
    $psi.EnvironmentVariables['OLLAMA_BASE_URL']     = 'http://localhost:11434'
    # Model preference is governed centrally in services/llm.py.

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi

    $stdoutAction = { Add-Content -Path $StdoutFile -Value $Event.SourceEventArgs.Data }
    $stderrAction = { Add-Content -Path $LogFile    -Value $Event.SourceEventArgs.Data }
    Register-ObjectEvent -InputObject $proc -EventName OutputDataReceived -Action $stdoutAction | Out-Null
    Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived  -Action $stderrAction | Out-Null

    $proc.Start()             | Out-Null
    $proc.BeginOutputReadLine()
    $proc.BeginErrorReadLine()

    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "[Watchdog $timestamp] Modernization backend started (PID $($proc.Id))" | Add-Content $LogFile
    Set-Content -LiteralPath $RuntimePidFile -Value $proc.Id
    return $proc
}

$backendProc = $null
while ($true) {
    Ensure-FrontendProductionSync
    Restart-BackendWhenSourceChanged
    if (-not (Is-BackendRunning)) {
        $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        "[Watchdog $timestamp] Port $Port not listening - starting Modernization backend..." | Add-Content $LogFile

        $zombie = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($zombie -and (Test-ModernizationProcess $zombie.OwningProcess)) {
            Stop-Process -Id $zombie.OwningProcess -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 2

        $backendProc = Start-Backend
        Start-Sleep -Seconds 10
    }
    Start-Sleep -Seconds $CheckSecs
}
