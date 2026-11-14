from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backend_auto_publish_fails_closed_and_preserves_secure_authentication():
    source = (ROOT / "watchdog_backend.ps1").read_text(encoding="utf-8")

    assert "if ($null -eq $activeJobs)" in source
    assert "deferring restart safely" in source
    assert "$psi.EnvironmentVariables['AUTH_REQUIRED']       = 'true'" in source
    assert "$BackendEnvironment.GetEnumerator()" in source
    assert "Set-Content -LiteralPath $RuntimePidFile" in source


def test_normal_launcher_starts_auto_publisher_by_default():
    source = (ROOT / "Start-Modernization.ps1").read_text(encoding="utf-8")

    assert "[switch]$NoAutoPublish" in source
    assert "Start-AutoPublish.ps1" in source
    assert "if (-not $NoAutoPublish)" in source


def test_frontend_sync_uses_production_build_and_excludes_generated_directories():
    source = (ROOT / "Sync-ProductionBuild.ps1").read_text(encoding="utf-8")

    assert "npm.cmd run build:production" in source
    assert "dist|node_modules" in source
