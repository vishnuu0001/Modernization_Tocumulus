from pathlib import Path
from dataclasses import replace

import pytest

from services.python_runtime import (
    SAFE_RUNTIME_ENV,
    enforce_safe_python_runtime,
    inspect_python_runtime,
)


ROOT = Path(__file__).resolve().parents[1]


def test_local_runtime_contract_requires_python_315():
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.15.2"
    launcher = (ROOT / "Start-Modernization.ps1").read_text(encoding="utf-8")
    database = (ROOT / "Initialize-ModernizationDatabase.ps1").read_text(encoding="utf-8")
    assert "StartsWith('3.15.')" in launcher
    assert "StartsWith('3.15.')" in database


def test_container_runtime_is_pinned_to_python_315():
    dockerfile = (ROOT / "docker" / "Dockerfile.standalone").read_text(encoding="utf-8")
    assert "FROM python:3.15.2-slim-bookworm AS python-runtime" in dockerfile
    assert "COPY --from=python-runtime /usr/local/ /usr/local/" in dockerfile
    assert "python3 python3-pip python3-venv" not in dockerfile


def test_pyyaml_is_upgraded_for_python_315_source_build():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "pyyaml==6.0.3" in requirements.casefold()


def test_runtime_profile_matches_the_qualified_execution_model():
    profile = inspect_python_runtime()
    assert profile.lazy_imports == "normal"
    assert profile.gil_enabled is True
    assert profile.free_threaded_build is False
    assert profile.jit_enabled is False
    assert enforce_safe_python_runtime(profile) is profile


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"lazy_imports": "all"}, "global lazy imports"),
        ({"gil_enabled": False}, "GIL must be enabled"),
        ({"free_threaded_build": True}, "standard GIL build"),
        ({"jit_enabled": True}, "experimental JIT"),
    ],
)
def test_runtime_contract_rejects_unqualified_modes(changes, message):
    profile = replace(inspect_python_runtime(), **changes)
    with pytest.raises(RuntimeError, match=message):
        enforce_safe_python_runtime(profile)


def test_runtime_policy_mapping_is_read_only_and_launchers_apply_it():
    assert dict(SAFE_RUNTIME_ENV) == {
        "PYTHON_LAZY_IMPORTS": "normal",
        "PYTHON_JIT": "0",
        "PYTHON_GIL": "1",
    }
    with pytest.raises(TypeError):
        SAFE_RUNTIME_ENV["PYTHON_JIT"] = "1"

    launcher = (ROOT / "Start-Modernization.ps1").read_text(encoding="utf-8")
    database = (ROOT / "Initialize-ModernizationDatabase.ps1").read_text(encoding="utf-8")
    dockerfile = (ROOT / "docker" / "Dockerfile.standalone").read_text(encoding="utf-8")
    for name, value in SAFE_RUNTIME_ENV.items():
        assert f"$env:{name} = '{value}'" in launcher
        assert f"$env:{name} = '{value}'" in database
        assert f"{name}={value}" in dockerfile


def test_application_source_does_not_opt_into_explicit_lazy_imports():
    for root in (ROOT / "api", ROOT / "services", ROOT / "tools"):
        for path in root.rglob("*.py"):
            for line in path.read_text(encoding="utf-8").splitlines():
                assert not line.lstrip().startswith(("lazy import ", "lazy from ")), path
