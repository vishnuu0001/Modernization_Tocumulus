[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$expectedRemote = "https://github.com/vishnuu0001/Modernization_Tocumulus.git"
$repositoryRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$gitScope = @("-c", "safe.directory=$repositoryRoot")

Push-Location -LiteralPath $repositoryRoot
try {
    $actualRoot = (& git @gitScope rev-parse --show-toplevel 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $actualRoot) {
        throw "This script must be run from a Git checkout."
    }

    $resolvedExpectedRoot = [System.IO.Path]::GetFullPath($repositoryRoot).TrimEnd('\', '/')
    $resolvedActualRoot = [System.IO.Path]::GetFullPath($actualRoot).TrimEnd('\', '/')
    if (-not $resolvedActualRoot.Equals($resolvedExpectedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Repository root mismatch: expected '$resolvedExpectedRoot', found '$resolvedActualRoot'."
    }

    $actualRemote = (& git @gitScope remote get-url origin 2>$null).Trim()
    if ($LASTEXITCODE -ne 0 -or $actualRemote -ne $expectedRemote) {
        throw "Refusing to enable auto-push: origin must be '$expectedRemote' (found '$actualRemote')."
    }

    & git @gitScope config --local core.hooksPath .githooks
    if ($LASTEXITCODE -ne 0) {
        throw "Could not configure core.hooksPath."
    }

    $configuredPath = (& git @gitScope config --local --get core.hooksPath).Trim()
    if ($configuredPath -ne ".githooks") {
        throw "Git hook configuration verification failed."
    }

    Write-Host "Auto-push enabled for $expectedRemote" -ForegroundColor Green
    Write-Host "Future commits will publish the current branch automatically."
    Write-Host "For an intentional local-only commit, set MODERNIZATION_SKIP_AUTO_PUSH=1 for that command."
}
finally {
    Pop-Location
}
