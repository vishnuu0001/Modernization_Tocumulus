[CmdletBinding()]
param(
    [string]$PostgresAdminUser = 'postgres',
    [SecureString]$PostgresAdminPassword,
    [string]$HostName = '127.0.0.1',
    [int]$Port = 5432
)

$ErrorActionPreference = 'Stop'
$moduleRoot = $PSScriptRoot
$pythonExe = Join-Path $moduleRoot '.venv\Scripts\python.exe'
$env:PYTHON_LAZY_IMPORTS = 'normal'
$env:PYTHON_JIT = '0'
$env:PYTHON_GIL = '1'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw "Python environment not found at $pythonExe. Create .venv with Python 3.15 and install requirements.txt."
}
$pythonVersion = (& $pythonExe -c "import platform; print(platform.python_version())").Trim()
if (-not $pythonVersion.StartsWith('3.15.')) {
    throw "Python 3.15 is required; $pythonExe is Python $pythonVersion. Recreate .venv with Python 3.15."
}
$psql = 'C:\Program Files\PostgreSQL\16\bin\psql.exe'
$createdb = 'C:\Program Files\PostgreSQL\16\bin\createdb.exe'
if (-not (Test-Path -LiteralPath $psql) -or -not (Test-Path -LiteralPath $createdb)) {
    throw 'PostgreSQL 16 command-line tools were not found.'
}
if (-not $PostgresAdminPassword) {
    $PostgresAdminPassword = Read-Host 'PostgreSQL administrator password' -AsSecureString
}
$credential = [pscredential]::new($PostgresAdminUser, $PostgresAdminPassword)
$plainAdminPassword = $credential.GetNetworkCredential().Password
$appPassword = -join ((1..48) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })

try {
    $env:PGPASSWORD = $plainAdminPassword
    $roleExists = & $psql -h $HostName -p $Port -U $PostgresAdminUser -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='modernization'"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to authenticate to PostgreSQL.' }
    if ($roleExists -eq '1') {
        & $psql -h $HostName -p $Port -U $PostgresAdminUser -d postgres -v ON_ERROR_STOP=1 -c "ALTER ROLE modernization WITH LOGIN PASSWORD '$appPassword'"
    } else {
        & $psql -h $HostName -p $Port -U $PostgresAdminUser -d postgres -v ON_ERROR_STOP=1 -c "CREATE ROLE modernization WITH LOGIN PASSWORD '$appPassword'"
    }
    if ($LASTEXITCODE -ne 0) { throw 'Unable to create the Modernization database role.' }
    $databaseExists = & $psql -h $HostName -p $Port -U $PostgresAdminUser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='modernization'"
    if ($databaseExists -ne '1') {
        & $createdb -h $HostName -p $Port -U $PostgresAdminUser -O modernization modernization
        if ($LASTEXITCODE -ne 0) { throw 'Unable to create the Modernization database.' }
    }

    $databaseUrl = "postgresql://modernization:$appPassword@$HostName`:$Port/modernization"
    $env:MODERNIZATION_DATABASE_URL = $databaseUrl
    $env:MODERNIZATION_BOOTSTRAP_ADMIN_USER = 'admin'
    $env:MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD = 'Admin@1234'
    & $pythonExe -c "from services.access_control import AccessStore; AccessStore().initialize()"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to initialize Modernization security tables.' }

    $envPath = Join-Path $moduleRoot '.env'
    $lines = @(
        "MODERNIZATION_DATABASE_URL=$databaseUrl",
        'MODERNIZATION_BOOTSTRAP_ADMIN_USER=admin',
        'MODERNIZATION_CREDENTIAL_PUBLIC_KEY=data/security/credential_public_key.pem'
    )
    [IO.File]::WriteAllLines($envPath, $lines)
    & icacls.exe $envPath /inheritance:r /grant:r "$env:USERNAME`:(R,W)" | Out-Null
    Write-Host "Modernization PostgreSQL database initialized. Configuration saved to $envPath."
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:MODERNIZATION_BOOTSTRAP_ADMIN_PASSWORD -ErrorAction SilentlyContinue
    $plainAdminPassword = $null
}
