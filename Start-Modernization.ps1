[CmdletBinding()]
param(
    [int]$Port = 18084,
    [switch]$SkipBuild,
    [switch]$NoAutoPublish
)

$ErrorActionPreference = 'Stop'
$moduleRoot = $PSScriptRoot
$pythonExe = Join-Path $moduleRoot '.venv\Scripts\python.exe'
$frontendDir = Join-Path $moduleRoot 'frontend'
$logDir = Join-Path $moduleRoot 'data\logs'
$pidFile = Join-Path $moduleRoot 'data\modernization.pid'
$stdoutLog = Join-Path $logDir 'standalone-stdout.log'
$stderrLog = Join-Path $logDir 'standalone-stderr.log'
$envFile = Join-Path $moduleRoot '.env'

if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*([^#][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            $name = $Matches[1]
            if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
                [Environment]::SetEnvironmentVariable($name, $Matches[2], 'Process')
            }
        }
    }
}

# Preserve the qualified eager-import, GIL-enabled, non-JIT execution model.
$env:PYTHON_LAZY_IMPORTS = 'normal'
$env:PYTHON_JIT = '0'
$env:PYTHON_GIL = '1'

if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python environment not found at $pythonExe. Create .venv and install requirements.txt."
}
$pythonVersion = (& $pythonExe -c "import platform; print(platform.python_version())").Trim()
if (-not $pythonVersion.StartsWith('3.14.')) {
    throw "Python 3.14 is required; $pythonExe is Python $pythonVersion. Recreate .venv with Python 3.14."
}

if ([string]::IsNullOrWhiteSpace($env:MODERNIZATION_DATABASE_URL)) {
    throw 'MODERNIZATION_DATABASE_URL is required, for example: postgresql://modernization:<password>@127.0.0.1:5432/modernization'
}
if ([string]::IsNullOrWhiteSpace($env:MODERNIZATION_BOOTSTRAP_ADMIN_USER)) {
    $env:MODERNIZATION_BOOTSTRAP_ADMIN_USER = 'admin'
}
if ([string]::IsNullOrWhiteSpace($env:OLLAMA_MODEL)) {
    $env:OLLAMA_MODEL = 'devstral-small-2:24b'
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    throw "Port $Port is already owned by PID $($listener.OwningProcess) ($($owner.Name)). Stop that process or choose -Port."
}

if (-not $SkipBuild) {
    Push-Location -LiteralPath $frontendDir
    try {
        if (-not (Test-Path -LiteralPath (Join-Path $frontendDir 'node_modules\.bin\vite.cmd'))) {
            & npm.cmd ci --include=dev
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
        }
        & npm.cmd run build:production
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:AUTH_REQUIRED = 'true'
$env:HOST = '127.0.0.1'
$env:PORT = [string]$Port
$env:CORS_ORIGINS = "http://localhost:$Port,http://127.0.0.1:$Port,http://localhost:5175,http://127.0.0.1:5175"

$process = Start-Process -FilePath $pythonExe `
    -ArgumentList @('-m', 'uvicorn', 'api.server:app', '--host', '127.0.0.1', '--port', [string]$Port, '--log-level', 'info') `
    -WorkingDirectory $moduleRoot `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -WindowStyle Hidden `
    -PassThru

$healthUrl = "http://127.0.0.1:$Port/api/health"
$deadline = (Get-Date).AddSeconds(30)
do {
    if ($process.HasExited) {
        throw "Modernization exited during startup. See $stderrLog"
    }
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.status -eq 'ok' -and $health.module -eq 'Modernization' -and $health.deployment -eq 'standalone') {
            $activeListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                Select-Object -First 1
            $runtimePid = if ($activeListener) { $activeListener.OwningProcess } else { $null }
            if (-not $runtimePid) {
                $listenerLine = & netstat.exe -ano | Select-String "127\.0\.0\.1:$Port\s+.*LISTENING\s+(\d+)" |
                    Select-Object -First 1
                if ($listenerLine -and $listenerLine.Matches.Count) {
                    $runtimePid = [int]$listenerLine.Matches[0].Groups[1].Value
                }
            }
            if (-not $runtimePid) { $runtimePid = $process.Id }
            Set-Content -LiteralPath $pidFile -Value $runtimePid
            if (-not $NoAutoPublish) {
                & (Join-Path $moduleRoot 'Start-AutoPublish.ps1') -Port $Port | Out-Host
            }
            Write-Host "Modernization standalone is running at http://127.0.0.1:$Port/ (PID $runtimePid)."
            return
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
} while ((Get-Date) -lt $deadline)

Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
throw "Modernization did not become healthy within 30 seconds. See $stderrLog"
