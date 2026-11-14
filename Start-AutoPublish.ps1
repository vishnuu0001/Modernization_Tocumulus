[CmdletBinding()]
param(
    [int]$Port = 18084
)

$ErrorActionPreference = 'Stop'
$moduleRoot = $PSScriptRoot
$watchdogScript = Join-Path $moduleRoot 'watchdog_backend.ps1'
$pidFile = Join-Path $moduleRoot 'data\backend-watchdog.pid'
$portFile = Join-Path $moduleRoot 'data\backend-watchdog.port'
$logDir = Join-Path $moduleRoot 'data\logs'

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

if (Test-Path -LiteralPath $pidFile) {
    $recorded = Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    $existing = if ($recorded) { Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$recorded)" -ErrorAction SilentlyContinue }
    # Only a powershell process running this module's watchdog script counts;
    # a recycled PID belonging to something else is ignored.
    if ($existing -and $existing.CommandLine -like '*watchdog_backend.ps1*') {
        $watchedPort = if (Test-Path -LiteralPath $portFile) {
            [int](Get-Content -LiteralPath $portFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        } else { 18084 }
        if ($watchedPort -eq $Port) {
            Write-Output "Automatic frontend/backend publishing is already running for port $Port (PID $recorded)."
            return
        }
        # The running watchdog publishes a different port, so the backend that
        # was just launched would never be republished. Replace it.
        Stop-Process -Id ([int]$recorded) -Force -ErrorAction SilentlyContinue
        Write-Output "Replaced automatic publisher for port $watchedPort (PID $recorded) with one for port $Port."
    }
}

$process = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $watchdogScript, '-Port', [string]$Port) `
    -WorkingDirectory $moduleRoot `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id
Set-Content -LiteralPath $portFile -Value $Port
Start-Sleep -Milliseconds 750
if ($process.HasExited) {
    throw "Automatic publisher exited during startup. Review data/logs/modernization_stderr.log."
}
Write-Output "Automatic frontend/backend publishing is running for port $Port (PID $($process.Id))."
