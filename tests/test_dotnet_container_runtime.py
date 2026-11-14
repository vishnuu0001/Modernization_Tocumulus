from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_standalone_image_configures_writable_dotnet_runtime_directory():
    dockerfile = (ROOT / "docker" / "Dockerfile.standalone").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "standalone-entrypoint.sh").read_text(encoding="utf-8")

    assert "XDG_RUNTIME_DIR=/tmp/dotnet-runtime-10001" in dockerfile
    assert '"${XDG_RUNTIME_DIR:-/tmp/dotnet-runtime-10001}"' in entrypoint
    assert 'chmod 0700 "${XDG_RUNTIME_DIR:-/tmp/dotnet-runtime-10001}"' in entrypoint


def test_standalone_entrypoint_precreates_dotnet_and_nuget_locations():
    entrypoint = (ROOT / "docker" / "standalone-entrypoint.sh").read_text(encoding="utf-8")

    assert '"${DOTNET_CLI_HOME:-/tmp/dotnet}"' in entrypoint
    assert '"${NUGET_PACKAGES:-/app/data/toolchains/nuget}"' in entrypoint
