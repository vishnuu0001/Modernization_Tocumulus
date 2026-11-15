# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: services/build_runner.py
# Date: 2026-07-19
# ---------------------------------------------------------------------------
"""
services/build_runner.py
Phase 2 of the code-generation pipeline: a real, whole-project compiler
build for generated C#/Java/TypeScript projects, feeding structured
per-file errors back into the REPAIR_PROMPT retry loop (see modernizer.py's
generate_from_prompt).

This is a materially different concern from services/validators.py's
per-file syntax checks: validators.py checks one file in isolation with no
dependency resolution (so it must filter out "cannot find symbol"-style
noise); this module builds the ENTIRE generated project with real package
restore (NuGet / Maven Central / npm), so a "member not found" or
"cannot find module" error here is genuine signal, not noise.

Requires .NET 8 SDK (`dotnet`), Maven (`mvn`), and Node/npm on PATH. Missing
tools degrade to checker="skipped" — this module never crashes the calling
job over a missing toolchain, matching the graceful-degradation pattern in
validators.py.
"""
from __future__ import annotations

import re
import json
import os
import importlib.util
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from services.native_toolchain import native_include_args
from services.tool_discovery import executable_environment, find_executable

_MVN_CMD = "mvn.cmd"
_JAVA_EXE = "java.exe" if os.name == "nt" else "java"
_JAVAC_EXE = "javac.exe" if os.name == "nt" else "javac"
_FLUTTER_BAT = "flutter.bat"
_PUBSPEC_YAML = "pubspec.yaml"
_POM_XML = "pom.xml"

_DOTNET_PATH = shutil.which("dotnet")
_MVN_PATH = shutil.which("mvn") or shutil.which(_MVN_CMD)
_NPM_PATH = shutil.which("npm") or shutil.which("npm.cmd")
_NPX_PATH = shutil.which("npx") or shutil.which("npx.cmd")

_BUILD_TIMEOUT = 180  # seconds — dotnet build / mvn compile
_NPM_INSTALL_TIMEOUT = 180  # seconds — real package-tree fetch
_NPM_BUILD_TIMEOUT = 420  # seconds — cold Angular/Vite/React production builds on Windows
_CONTAINER_BUILD_TIMEOUT = 900  # seconds — cold native dependency graphs in Linux containers
_TOOLCHAIN_CRASH_RETRIES = 2  # extra attempts after a detected transient npm/node crash
_TOOLCHAIN_CRASH_BACKOFF = 5  # seconds between retries — lets transient memory pressure clear
# Signatures of npm/node crashing itself before it ever reaches user code — a
# host-level allocator/memory-pressure blip (observed in production under a
# constrained Windows service account), not a defect in the generated
# package.json/source. Treated as retryable; anything else (missing package,
# bad script, real TS/compile errors) is left alone and reported immediately.
_TRANSIENT_TOOLCHAIN_SIGNATURES = (
    "virtualalloc failed",
    "low_level_alloc",
    "fatal error: javascript heap out of memory",
    "fatal error in v8",
    "enomem",
    "cannot allocate memory",
    "econnreset",
    "eai_again",
    "socket hang up",
    "network timeout",
)
_TSC_VALIDATOR = Path(__file__).resolve().parent.parent / "tools" / "ts-validate" / "node_modules" / "typescript" / "lib" / "tsc.js"
_BUILD_KEY = "<build>"
_DEPENDENCY_COMPATIBILITY_KEY = "<dependency-compatibility>"
_INSTALL_KEY = "<install>"
_PACKAGE_JSON = "package.json"
_CLANG = "clang"


def _run_process_tree(
    command: list[str], *, cwd: str, timeout: int, env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a tool with timeout cleanup that includes launcher descendants.

    On Windows, package-manager ``.bat``/``.cmd`` launchers can exit while a
    Ruby, Node, Java, or PHP child still owns the captured stdout/stderr pipe.
    ``subprocess.run(..., timeout=...)`` kills only the launcher and can then
    wait forever for that orphaned child. Start a distinct process group and
    terminate the exact tree before collecting output so the build gate always
    returns a bounded diagnostic.
    """
    windows_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=windows_flags,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            stdout, stderr = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command, timeout, output=stdout or exc.output, stderr=stderr or exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
_CLANG_CPP = "clang++"
_MISSING_MANIFEST = "missing-manifest"
_MISSING_TOOLCHAIN = "missing-toolchain"
_NPM_TSC = "npm-tsc"
_MAVEN_REPOSITORY = Path(
    os.getenv("MODERNIZATION_MAVEN_REPOSITORY")
    or (Path(__file__).resolve().parent.parent / "data" / "toolchains" / "maven-repository")
)
_MAVEN_REPOSITORY_FAILURES = (
    "access is denied", "permission denied", "invalid loc header",
    "zip end header not found", "error in opening zip file", "checksum validation failed",
)

_TOOLCHAIN_REQUIREMENTS = (
    ("COBOL", ("cobol",), (("cobc",),)),
    ("C++", ("c++", "cpp"), ((_CLANG_CPP, "g++"),)),
    ("C", ("c17 native", "language:c"), ((_CLANG, "gcc"),)),
    ("Go", ("golang", "go gin", "go rest", "language:go"), (("go",), ("gofmt",))),
    ("PHP", ("php", "laravel"), (("php",),)),
    ("Ruby", ("ruby", "rails"), (("ruby",),)),
    ("Node.js", ("node.js", "express", "react", "typescript", "javascript"), (("node",), ("npm",))),
)
_TYPESCRIPT_SIGNALS = ("typescript", "javascript", "react", "angular", "vue", "node.js")
_SQL_SIGNALS = ("sql", "postgres", "oracle", "db2", "mysql", "mssql")

SOURCE_BUILD_LANGUAGES = frozenset({
    "python", "javascript", "c", "cpp", "cobol", "php", "ruby", "go",
})
EXTENDED_BUILD_LANGUAGES = frozenset({
    "rust", "swift", "kotlin", "shell", "r", "scala", "clojure",
    "haskell", "lisp", "elixir", "dart", "julia", "hcl", "protobuf",
    "fortran", "ada", "pascal", "erlang", "ocaml", "prolog", "abap",
    "pli", "rpg", "jcl", "mumps", "natural", "progress4gl", "apex",
    "jenkinsfile",
})
ARTIFACT_BUILD_LANGUAGES = frozenset({
    "yaml", "json", "toml", "xml", "graphql", "dockerfile",
    "cloudformation", "kubernetes", "helm", "ansible",
    "github_actions", "markdown", "sql", "hcl", "protobuf", "jenkinsfile",
})
PROJECT_BUILD_LANGUAGES = frozenset({
    "csharp", "java", "typescript", "javascript", "python", "go",
    "kotlin", "rust", "php", "dart", "swift", "scala", "clojure", "shell",
    "r", "julia", "haskell", "lisp", "c", "cpp", "cobol", "ruby",
    "elixir", "erlang", "sql",
}) | ARTIFACT_BUILD_LANGUAGES
# Only languages with an actual dependency-aware dispatcher belong here.
# Syntax-only artifact and vendor-heuristic validators remain available for
# single files, but are never advertised as production project build routes.
PRODUCTION_PROJECT_BUILD_LANGUAGES = PROJECT_BUILD_LANGUAGES


def _merge_path_values(values: list[str]) -> tuple[list[str], set[str]]:
    entries: list[str] = []
    seen: set[str] = set()
    for value in values:
        for entry in value.split(os.pathsep):
            stripped = entry.strip()
            normalized = stripped.rstrip("\\").casefold()
            if normalized and normalized not in seen:
                seen.add(normalized)
                entries.append(stripped)
    return entries, seen


def _prepend_existing_msys_paths(entries: list[str], seen: set[str]) -> None:
    for candidate in (r"C:\msys64\ucrt64\bin", r"C:\msys64\usr\bin"):
        normalized = candidate.rstrip("\\").casefold()
        if os.path.isdir(candidate) and normalized not in seen:
            seen.add(normalized)
            entries.insert(0, candidate)


# Function: _refresh_windows_path
def _refresh_windows_path() -> None:
    """Merge current registry PATH values into long-running service processes."""
    if os.name != "nt":
        return
    try:
        import winreg
        locations = (
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, r"Environment"),
        )
        values = [os.environ.get("PATH", "")]
        for hive, key_name in locations:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                values.append(os.path.expandvars(str(value)))
        entries, seen = _merge_path_values(values)
        # Prefer known MSYS2 bins when present so service restarts are not required
        # after manual runtime installs done outside of machine/user PATH updates.
        _prepend_existing_msys_paths(entries, seen)
        os.environ["PATH"] = os.pathsep.join(entries)
    except (OSError, ValueError):
        pass


# Function: _refresh_tool_paths
def _refresh_tool_paths() -> None:
    """Refresh cached executable paths after long-running services update PATH."""
    global _DOTNET_PATH, _MVN_PATH, _NPM_PATH, _NPX_PATH
    _refresh_windows_path()
    _DOTNET_PATH = shutil.which("dotnet")
    _MVN_PATH = _which("mvn")
    _NPM_PATH = shutil.which("npm") or shutil.which("npm.cmd")
    _NPX_PATH = shutil.which("npx") or shutil.which("npx.cmd")


# Function: _which
def _first_existing_file(candidates) -> str | None:
    return next((str(candidate) for candidate in candidates if candidate.is_file()), None)


def _java_fallback(command_key: str) -> str | None:
    java_home = _preferred_java_home()
    if not java_home:
        return None
    binary = "javac.exe" if "javac" in command_key else _JAVA_EXE
    return _first_existing_file((java_home / "bin" / binary,))


def _maven_fallback() -> str | None:
    maven_home = os.getenv("MAVEN_HOME")
    if maven_home:
        resolved = _first_existing_file(
            Path(maven_home) / "bin" / binary
            for binary in (_MVN_CMD, "mvn.exe", "mvn.bat")
        )
        if resolved:
            return resolved
    tools_root = Path(r"C:\Tools")
    if not tools_root.is_dir():
        return None
    return _first_existing_file(
        sorted(tools_root.glob(f"apache-maven-*/bin/{_MVN_CMD}"), reverse=True)
    )


def _gradle_fallback() -> str | None:
    gradle_home = os.getenv("GRADLE_HOME")
    if gradle_home:
        resolved = _first_existing_file(
            Path(gradle_home) / "bin" / binary
            for binary in ("gradle.bat", "gradle.cmd", "gradle.exe")
        )
        if resolved:
            return resolved
    candidates = (
        candidate
        for root in (Path(r"C:\Gradle"), Path(r"C:\ProgramData\chocolatey\lib"))
        if root.is_dir()
        for candidate in sorted(root.glob("**/gradle*.bat"), reverse=True)
    )
    return _first_existing_file(candidates)


def _flutter_fallback() -> str | None:
    user_home = Path(os.path.expanduser("~"))
    return _first_existing_file((
        user_home / ".puro" / "envs" / "stable" / "flutter" / "bin" / _FLUTTER_BAT,
        user_home / ".puro" / "shared" / "flutter" / "bin" / _FLUTTER_BAT,
    ))


def _php_fallback(command: str) -> str | None:
    local_app_data = os.getenv("LOCALAPPDATA")
    if command.lower() != "php" or not local_app_data:
        return None
    packages = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    return _first_existing_file(sorted(packages.glob("PHP.PHP.8.3_*/*php.exe"), reverse=True))


def _windows_command_fallback(command: str, command_key: str) -> str | None:
    if command_key in {"java", _JAVA_EXE, "javac", "javac.exe"}:
        return _java_fallback(command_key)
    if command_key in {"mvn", _MVN_CMD, "mvn.exe"}:
        return _maven_fallback()
    if command_key in {"gradle", "gradle.bat", "gradle.cmd", "gradle.exe"}:
        return _gradle_fallback()
    if command_key in {"flutter", _FLUTTER_BAT, "flutter.cmd", "flutter.exe"}:
        return _flutter_fallback()
    return _php_fallback(command)


def _which(command: str) -> str | None:
    """Resolve a command, including per-user WinGet packages hidden by stale machine PATH entries."""
    command_key = (command or "").strip().casefold()
    if os.name == "nt" and command_key in {"java", _JAVA_EXE, "javac", "javac.exe"}:
        preferred = _java_fallback(command_key)
        if preferred:
            return preferred
    resolved = find_executable(command)
    if resolved or os.name != "nt":
        return resolved
    return _windows_command_fallback(command, command_key)


# Function: _command_usable
def _command_usable(command: str) -> bool:
    """Return true only when an executable can actually start successfully."""
    command_key = command.casefold()
    path = _which(command)
    if not path:
        return False

    # Some MSYS2 tools are shell launchers (not Win32 PE executables) and can
    # only be reliably probed via bash -lc from Windows service processes.
    def _msys_bash_probe() -> bool:
        if os.name != "nt" or "msys64" not in path.casefold() or command_key not in {"go", "rscript"}:
            return False
        bash = _which("bash") or r"C:\msys64\usr\bin\bash.exe"
        if not os.path.isfile(bash):
            return False
        probe = {
            "go": "/ucrt64/bin/go version",
            "rscript": "/ucrt64/bin/Rscript --version",
        }[command_key]
        msys_env = os.environ.copy()
        msys_env.setdefault("MSYSTEM", "UCRT64")
        msys_env.setdefault("CHERE_INVOKING", "1")
        try:
            retry = subprocess.run(
                [bash, "-lc", probe],
                capture_output=True,
                text=True,
                timeout=30,
                env=msys_env,
            )
            return retry.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    arguments = {
        "go": ["version"],
        "gofmt": ["-h"],
        "php": ["-v"],
        "ruby": ["--version"],
        "cobc": ["--version"],
        "mvn": ["-version"],
        "sqlcmd": ["-?"],
        "sqlite3": ["--version"],
        "npm": ["--version"],
        "node": ["--version"],
        "javac": ["-version"],
        "kotlinc": ["-version"],
        "scalac": ["-version"],
        "gradle": ["--version"],
        "sbt": ["--version"],
        "composer": ["--version"],
        "bundle": ["--version"],
        "flutter": ["--version"],
        "cabal": ["--version"],
        "stack": ["--version"],
        "erl": ["-noshell", "-eval", "halt()."],
        "elixirc": ["--version"],
        "mix": ["--version"],
        "fpc": ["-h"],
        "erlc": ["-v"],
        "rscript": ["--version"],
    }.get(command_key, ["--version"])
    try:
        # Some SDK launchers (notably Flutter and first-run JVM tools) need
        # longer than a trivial compiler probe, and Swift needs its runtime
        # directory when discovered outside the process PATH.
        proc = subprocess.run(
            [path, *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            env=executable_environment(path),
        )
        if command_key == "gofmt":
            return proc.returncode in {0, 2}
        if proc.returncode == 0:
            return True
        return _msys_bash_probe()
    except (OSError, subprocess.SubprocessError):
        return _msys_bash_probe()


@dataclass
class BuildResult:
    passed: bool
    checker: str                        # "dotnet" | "maven" | "npm-tsc" | "skipped"
    errors_by_file: dict[str, list[str]] = field(default_factory=dict)
    raw_output: str = ""


# Function: installed_dotnet_majors
def installed_dotnet_majors() -> list[int]:
    """Return installed SDK major versions; an empty list means unavailable."""
    if not _DOTNET_PATH:
        return []
    try:
        proc = subprocess.run([_DOTNET_PATH, "--list-sdks"], capture_output=True, text=True, timeout=15)
        return sorted({int(match.group(1)) for match in re.finditer(r"(?m)^(\d+)\.", proc.stdout)})
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


# Function: installed_java_majors
def installed_java_majors() -> list[int]:
    versions = {
        version for home in _java_home_candidates()
        if (version := _java_home_major(home)) is not None
    }
    java = shutil.which("java")
    if java:
        try:
            proc = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=10)
            match = re.search(r'version "(?:1\.)?(\d+)', proc.stderr + proc.stdout)
            if match:
                versions.add(int(match.group(1)))
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted(versions)


def _java_home_candidates() -> list[Path]:
    homes: list[Path] = []
    configured = os.environ.get("JAVA_HOME")
    if configured:
        homes.append(Path(configured))
    homes.extend(Path(value) for value in os.environ.get("MODERNIZATION_JAVA_HOMES", "").split(os.pathsep) if value)
    if os.name == "nt":
        homes.extend(Path(r"C:\Program Files\Eclipse Adoptium").glob("jdk-*"))
        homes.extend((Path.home() / ".jdks").glob("jdk-*"))
    else:
        homes.extend(Path("/opt/jdks").glob("*"))
        homes.extend(Path("/usr/lib/jvm").glob("*"))
    valid, seen = [], set()
    for home in homes:
        identity = str(home)
        if identity not in seen and (home / "bin" / _JAVA_EXE).is_file() and (home / "bin" / _JAVAC_EXE).is_file():
            valid.append(home)
            seen.add(identity)
    return valid


def _java_home_major(home: Path) -> int | None:
    # Debian names legacy-compatible JDK 21 paths `java-1.21.0-*`; match the
    # actual feature release before the generic `java-<major>` directory form.
    legacy = re.search(r"java-1\.(\d+)(?:[.+_-]|$)", home.name, re.IGNORECASE)
    if legacy:
        return int(legacy.group(1))
    match = re.search(r"(?:jdk-|java-)?(\d+)(?:[.+_-]|$)", home.name, re.IGNORECASE)
    if match:
        return int(match.group(1))
    try:
        proc = subprocess.run(
            [str(home / "bin" / _JAVA_EXE), "-version"],
            capture_output=True, text=True, timeout=10,
        )
        version = re.search(r'version "(?:1\.)?(\d+)', proc.stderr + proc.stdout)
        return int(version.group(1)) if version else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _java_home_for_major(major: int) -> Path | None:
    return next((home for home in _java_home_candidates() if _java_home_major(home) == major), None)


def _node_home_runtime(home: Path) -> tuple[Path, Path, Path] | None:
    """Resolve a portable Node home on Linux or Windows."""
    layouts = (
        (home / "bin" / "node", home / "bin" / "npm", home / "bin" / "npx"),
        (home / "node.exe", home / "npm.cmd", home / "npx.cmd"),
    )
    return next((layout for layout in layouts if all(path.is_file() for path in layout)), None)


def _node_home_candidates(major: int) -> list[Path]:
    configured = os.getenv(f"MODERNIZATION_NODE{major}_HOME")
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend((
        Path(f"/opt/node/{major}"),
        Path(__file__).resolve().parent.parent / "data" / "toolchains" / f"node{major}",
    ))
    return candidates


def _node_runtime_for_major(major: int) -> tuple[Path, Path, Path] | None:
    return next(
        (runtime for home in _node_home_candidates(major) if (runtime := _node_home_runtime(home))),
        None,
    )


def installed_node_majors() -> list[int]:
    candidates = [Path("/usr/local/bin/node")]
    for major in (20, 22, 24, 26):
        runtime = _node_runtime_for_major(major)
        if runtime:
            candidates.insert(0, runtime[0])
    current = shutil.which("node")
    if current:
        candidates.append(Path(current))
    versions, seen = set(), set()
    for executable in candidates:
        if str(executable) in seen or not executable.is_file():
            continue
        seen.add(str(executable))
        try:
            proc = subprocess.run([str(executable), "--version"], capture_output=True, text=True, timeout=10)
            match = re.search(r"v?(\d+)", proc.stdout)
            if proc.returncode == 0 and match:
                versions.add(int(match.group(1)))
        except (OSError, subprocess.SubprocessError):
            continue
    return sorted(versions)


def _project_node_major(package_path: Path) -> int:
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return 22
    dependencies = {**(package.get("dependencies") or {}), **(package.get("devDependencies") or {})}
    angular_version = dependencies.get("@angular/core") or dependencies.get("@angular/cli")
    match = re.search(r"(\d+)", str(angular_version or ""))
    if match:
        return 20 if int(match.group(1)) <= 18 else 22
    return 22


def _npm_runtime(package_path: Path) -> tuple[str, str, dict[str, str]]:
    major = _project_node_major(package_path)
    runtime = _node_runtime_for_major(major)
    if not runtime:
        # Windows developer checkouts continue to use their provisioned LTS
        # Node. The standalone image is required to provide both pinned homes.
        fallback_node = shutil.which("node") or ""
        fallback_npm = shutil.which("npm") or _NPM_PATH or ""
        fallback_npx = shutil.which("npx") or _NPX_PATH or ""
        return fallback_npm, fallback_npx, executable_environment(fallback_node) if fallback_node else os.environ.copy()
    node, npm, npx = runtime
    environment = os.environ.copy()
    environment["PATH"] = str(node.parent) + os.pathsep + environment.get("PATH", "")
    return str(npm), str(npx), environment


# Function: _preferred_java_home
def _preferred_java_home() -> Path | None:
    configured = os.environ.get("JAVA_HOME")
    if configured:
        home = Path(configured)
        if (home / "bin" / _JAVA_EXE).is_file() and (home / "bin" / _JAVAC_EXE).is_file():
            return home
    candidates = [(_java_home_major(home) or 0, home) for home in _java_home_candidates()]
    return max(candidates, default=(0, None), key=lambda item: item[0])[1]


# Function: toolchain_compatibility_error
def toolchain_compatibility_error(stack_description: str) -> str | None:
    """Fail before generation when the requested .NET target cannot be built here."""
    _refresh_tool_paths()
    text = (stack_description or "").lower()
    checks = (
        _dotnet_compatibility_error(stack_description),
        _java_compatibility_error(stack_description, text),
        _node_compatibility_error(stack_description),
        _required_toolchain_error(text),
        _parser_compatibility_error(text),
    )
    return next((error for error in checks if error), None)


# Function: _dotnet_compatibility_error
def _dotnet_compatibility_error(stack_description: str) -> str | None:
    match = re.search(r"(?:\.net|dotnet|net)\s*(\d+)", stack_description or "", re.IGNORECASE)
    if not match:
        return None
    requested = int(match.group(1))
    installed = installed_dotnet_majors()
    if requested in installed:
        return None
    available = ", ".join(f".NET {version}" for version in installed) or "none"
    return (
        f"Target .NET {requested} cannot be validated on this build host. Installed SDKs: {available}. "
        f"Install the .NET {requested} SDK or revise and approve the plan for an installed target before transformation."
    )


# Function: _java_compatibility_error
def _java_compatibility_error(stack_description: str, normalized: str) -> str | None:
    match = re.search(r"java\s*(\d+)", stack_description or "", re.IGNORECASE)
    installed = installed_java_majors()
    if match:
        requested = int(match.group(1))
        supported = {8, 11, 17, 21}
        if requested < 17 and re.search(r"spring\s*boot\s*3", normalized):
            return f"Spring Boot 3 requires Java 17 or later; Java {requested} is not a compatible target."
        if requested not in supported or requested not in installed:
            available = ", ".join(f"Java {version}" for version in installed) or "none"
            return (
                f"Target Java {requested} is outside this image's supported JDK releases {sorted(supported)} "
                f"or its matching JDK is missing. Installed JDKs: {available}."
            )
    if "java" not in normalized:
        return None
    missing: list[str] = []
    if not _command_usable("javac"):
        missing.append("javac")
    if not (_command_usable("mvn") or _command_usable("gradle")):
        missing.append("mvn/gradle")
    return _missing_prerequisites_error("Java", missing)


_ANGULAR_NODE_MAJOR_COMPATIBILITY = {
    17: frozenset({18, 20}),
    18: frozenset({18, 20, 22}),
    19: frozenset({18, 20, 22}),
    20: frozenset({20, 22, 24}),
    21: frozenset({20, 22, 24}),
    22: frozenset({22, 24, 26}),
}


def _angular_node_runtime_available(angular_major: int) -> bool:
    compatible = _ANGULAR_NODE_MAJOR_COMPATIBILITY.get(angular_major, frozenset())
    return bool(compatible.intersection(installed_node_majors()))


def _node_compatibility_error(stack_description: str) -> str | None:
    return node_compatibility_error(stack_description, installed_node_majors())


def node_compatibility_error(
    stack_description: str, installed_node_versions: list[int] | tuple[int, ...] | set[int],
) -> str | None:
    """Validate a target against an explicit Node inventory.

    Keeping the inventory injectable lets the API readiness catalog use the
    same compatibility policy as the build gate instead of merely checking
    that some npm executable exists.
    """
    text = stack_description or ""
    angular = re.search(r"angular\s*(\d+)", text, re.IGNORECASE)
    react = re.search(r"react(?!\s*native)\s*(\d{1,2})(?![\d.])", text, re.IGNORECASE)
    if angular:
        major = int(angular.group(1))
        if major not in range(17, 23):
            return "Angular scaffold support is bounded to majors 17–22."
        compatible_node = _ANGULAR_NODE_MAJOR_COMPATIBILITY[major]
    elif react:
        major = int(react.group(1))
        if major not in {16, 17, 18, 19}:
            return "React scaffold support is bounded to majors 16–19."
        required_node = 22
    else:
        return None
    installed = sorted({int(version) for version in installed_node_versions})
    if angular:
        if compatible_node.intersection(installed):
            return None
        compatible_label = ", ".join(str(version) for version in sorted(compatible_node))
        return (
            f"Angular {angular.group(1)} requires a compatible installed Node.js major "
            f"from [{compatible_label}]; installed Node.js majors are {installed}."
        )
    required_node = 22
    if required_node in installed:
        return None
    label = f"Angular {angular.group(1)}" if angular else f"React {react.group(1)}"
    return f"{label} requires installed Node.js {required_node}; installed Node.js majors are {installed}."


# Function: _missing_prerequisites_error
def _missing_prerequisites_error(label: str, missing: list[str]) -> str | None:
    if not missing:
        return None
    return f"Target {label} cannot be strictly validated on this build host. Missing prerequisite(s): {', '.join(missing)}."


# Function: _required_toolchain_error
def _required_toolchain_error(normalized: str) -> str | None:
    for label, signals, command_groups in _TOOLCHAIN_REQUIREMENTS:
        if not any(signal in normalized for signal in signals):
            continue
        missing = [
            "/".join(group)
            for group in command_groups
            if not any(_command_usable(command) for command in group)
        ]
        error = _missing_prerequisites_error(label, missing)
        if error:
            return error
    return None


# Function: _parser_compatibility_error
def _parser_compatibility_error(normalized: str) -> str | None:
    if any(signal in normalized for signal in _TYPESCRIPT_SIGNALS) and not _TSC_VALIDATOR.is_file():
        return "Target TypeScript/JavaScript cannot be strictly validated: the vendored TypeScript compiler is missing."
    if any(signal in normalized for signal in _SQL_SIGNALS) and importlib.util.find_spec("sqlglot") is None:
        return "Target SQL cannot be strictly validated: the sqlglot parser is missing from the backend environment."
    return None


def _all_commands_usable(*commands: str) -> bool:
    return all(_command_usable(command) for command in commands)


def _java_status_path() -> str:
    return str(_preferred_java_home() or shutil.which("java") or "")


def _any_command_usable(*commands: str) -> bool:
    return any(_command_usable(command) for command in commands)


def _first_shutil_path(*commands: str) -> str | None:
    return next((path for command in commands if (path := shutil.which(command))), None)


def _first_tool_path(tools: dict, *names: str) -> str | None:
    return next((tools[name]["path"] for name in names if tools[name]["path"]), None)


def _any_tool_ready(tools: dict, *names: str) -> bool:
    return any(tools[name]["ready"] for name in names)


def _all_tools_ready(tools: dict, *names: str) -> bool:
    return all(tools[name]["ready"] for name in names)


# Function: toolchain_status
def toolchain_status() -> dict:
    """Serializable readiness inventory used before planning and generation."""
    _refresh_tool_paths()
    dotnet = installed_dotnet_majors()
    java = installed_java_majors()
    tools = {
        "dotnet": {"ready": bool(dotnet), "versions": [str(v) for v in dotnet], "supports": [f"net{v}.0" for v in dotnet]},
        "node": {"ready": _command_usable("node"), "versions": [str(v) for v in installed_node_majors()], "path": shutil.which("node")},
        "npm": {"ready": bool(_all_commands_usable("npm") and _NPX_PATH), "path": _NPM_PATH},
        "java": {"ready": bool(java) and _all_commands_usable("javac"), "versions": [str(v) for v in java], "path": _java_status_path()},
        "maven": {"ready": _command_usable("mvn"), "path": _MVN_PATH},
        "python": {"ready": bool(shutil.which("python")), "path": shutil.which("python")},
        "go": {"ready": _all_commands_usable("go", "gofmt"), "path": _which("go")},
        "php": {"ready": _command_usable("php"), "path": _which("php")},
        "ruby": {"ready": _command_usable("ruby"), "path": _which("ruby")},
        "bundler": {"ready": _command_usable("bundle"), "path": _which("bundle")},
        "c": {"ready": _any_command_usable(_CLANG, "gcc"), "path": _first_shutil_path(_CLANG, "gcc")},
        "cpp": {"ready": _any_command_usable(_CLANG_CPP, "g++"), "path": _first_shutil_path(_CLANG_CPP, "g++")},
        "cobol": {"ready": _command_usable("cobc"), "path": shutil.which("cobc")},
        "typescript_validator": {"ready": _all_commands_usable("node") and _TSC_VALIDATOR.is_file(), "path": str(_TSC_VALIDATOR)},
        "sql_parser": {"ready": importlib.util.find_spec("sqlglot") is not None, "path": "python:sqlglot"},
        "sql_dialects": {
            "ready": importlib.util.find_spec("sqlglot") is not None and importlib.util.find_spec("sqlfluff") is not None,
            "dialects": ["postgres", "mysql", "sqlite", "tsql", "oracle", "db2", "bigquery", "snowflake", "redshift", "duckdb", "databricks", "spark", "hive", "trino", "presto", "clickhouse", "teradata"],
            "path": "python:sqlglot+sqlfluff",
            "note": "Static dialect parsing only; Oracle/Db2/cloud database engines remain external services.",
        },
        "database_clients": {
            "ready": all((_command_usable("psql"), _any_command_usable("mysql", "mariadb"), _command_usable("sqlcmd"), _command_usable("sqlite3"))),
            "postgresql": {"ready": _command_usable("psql"), "path": _which("psql")},
            "mysql": {"ready": _any_command_usable("mysql", "mariadb"), "path": _first_shutil_path("mysql", "mariadb")},
            "sql_server": {"ready": _command_usable("sqlcmd"), "path": _which("sqlcmd")},
            "sqlite": {"ready": _command_usable("sqlite3"), "path": _which("sqlite3")},
            "note": "Clients are installed; only PostgreSQL runs in the standalone Compose stack. Other database engines require external endpoints.",
        },
        # db2_sql_parser was checked by _stack_readiness() in api/server.py but never
        # defined here, unconditionally gating db2_sql/cobol_db2 regardless of what
        # was actually installed. sqlfluff (already a pinned dependency) ships a
        # native "db2" dialect - that's the real parser validators.py._validate_sql
        # already prefers for DB2 SQL, this just makes readiness match reality.
        "db2_sql_parser": {"ready": importlib.util.find_spec("sqlfluff") is not None, "path": "python:sqlfluff"},
        "yaml_parser": {"ready": importlib.util.find_spec("yaml") is not None, "path": "python:yaml"},
        "json_parser": {"ready": True, "path": "python:json"},
        "xml_parser": {"ready": True, "path": "python:xml.etree.ElementTree"},
        "toml_parser": {"ready": importlib.util.find_spec("tomllib") is not None, "path": "python:tomllib"},
        "graphql_parser": {"ready": importlib.util.find_spec("graphql") is not None, "path": "python:graphql"},
        "terraform": {"ready": importlib.util.find_spec("hcl2") is not None, "path": "python:hcl2"},
        "protobuf": {"ready": _command_usable("protoc"), "path": shutil.which("protoc")},
        "shell": {"ready": _command_usable("bash"), "path": _which("bash")},
        # No external grammar for Jenkinsfile (Groovy-based pipeline DSL) exists as a
        # dependency here; validated via a structural check (see validators.py), same
        # honesty tier as the heuristic-validated legacy languages below - always
        # "ready" since it has no external tool to be missing.
        "jenkinsfile": {"ready": True, "path": "heuristic:jenkinsfile"},
        # ── Additional language toolchains (validators.py._EXTERNAL_VALIDATORS
        # already has real compiler invocations wired for all of these - they were
        # gated purely because no readiness key existed here, same bug as db2_sql_parser
        # above, not because the check itself was ever missing. ──
        "rust": {"ready": _command_usable("rustc"), "path": _which("rustc")},
        "swift": {"ready": _command_usable("swiftc"), "path": _which("swiftc")},
        "kotlin": {"ready": _command_usable("kotlinc"), "path": _which("kotlinc")},
        "gradle": {"ready": _command_usable("gradle"), "path": _which("gradle")},
        "r": {"ready": _command_usable("Rscript"), "path": _which("Rscript")},
        "scala": {"ready": _command_usable("scalac"), "path": _which("scalac")},
        "sbt": {"ready": _command_usable("sbt"), "path": _which("sbt")},
        # Stack-managed GHC installations intentionally need not expose ghc on
        # the service PATH; Stack is itself a complete compiler/build route.
        "haskell": {
            "ready": _any_command_usable("ghc", "stack"),
            "path": next((path for command in ("ghc", "stack") if (path := _which(command))), None),
        },
        "cabal": {"ready": _command_usable("cabal"), "path": _which("cabal")},
        "stack": {"ready": _command_usable("stack"), "path": _which("stack")},
        "elixir": {"ready": _command_usable("elixirc"), "path": _which("elixirc")},
        "mix": {"ready": _command_usable("mix"), "path": _which("mix")},
        "dart": {"ready": _command_usable("dart"), "path": _which("dart")},
        "flutter": {"ready": _command_usable("flutter"), "path": _which("flutter")},
        "julia": {"ready": _command_usable("julia"), "path": _which("julia")},
        "fortran": {"ready": _any_command_usable("flang-new", "gfortran"),
                "path": _first_shutil_path("flang-new", "gfortran")},
        "ada": {"ready": _command_usable("gnatmake"), "path": shutil.which("gnatmake")},
        "pascal": {"ready": _command_usable("fpc"), "path": shutil.which("fpc")},
        "erlang": {
            "ready": _all_commands_usable("erlc", "erl"),
            "path": _which("erlc"),
        },
        # DkML's ocamlc on this box has a Visual Studio version incompatibility
        # ("has a version 18.0 not supported by DkML") unrelated to anything
        # changed here - genuinely not ready, not another instance of the wiring bug.
        "ocaml": {"ready": _command_usable("ocamlc"), "path": shutil.which("ocamlc"),
                  "note": "Installed but currently broken: DkML/Visual Studio version mismatch."},
        "prolog": {"ready": _command_usable("swipl"), "path": shutil.which("swipl")},
        "lisp": {"ready": _command_usable("sbcl"), "path": _which("sbcl")},
        # No entry in _EXTERNAL_VALIDATORS (dispatches straight to the tree-sitter
        # grammar instead, already real syntax validation) - "ready" here reflects
        # the standalone `clojure` CLI actually being usable, not a validation gap.
        "clojure": {"ready": _command_usable("clojure"), "path": _which("clojure")},
        "composer": {"ready": _command_usable("composer"), "path": _which("composer")},
        # No open-source equivalent exists for these 8 - see validators.py's
        # _UNAVAILABLE_VENDOR_TOOLCHAINS - covered instead by heuristic structural
        # validators below (key names match _STACK_LANGUAGE_TOOL in api/server.py
        # exactly), always "ready" since there is no external tool to be missing.
        "abap": {"ready": True, "path": "heuristic:abap"},
        "rpg": {"ready": True, "path": "heuristic:rpg"},
        "jcl": {"ready": True, "path": "heuristic:jcl"},
        "mumps": {"ready": True, "path": "heuristic:mumps"},
        "natural": {"ready": True, "path": "heuristic:natural"},
        "progress4gl": {"ready": True, "path": "heuristic:progress4gl"},
        "apex": {"ready": True, "path": "heuristic:apex"},
        "pli": {"ready": True, "path": "heuristic:pli"},
        "git": {"ready": bool(shutil.which("git")), "path": shutil.which("git"),
                "note": "Optional import/export integration; governed snapshots remain authoritative."},
        "docker": {"ready": bool(shutil.which("docker")), "path": shutil.which("docker"),
                   "note": "Optional; native SDK builds remain available when Docker is stopped."},
    }
    tools["rust_package_manager"] = {
        "ready": _command_usable("cargo"), "path": _which("cargo"),
    }
    tools["swift_package_manager"] = {
        "ready": _command_usable("swift"), "path": _which("swift"),
    }
    tools["jvm_build"] = {
        "ready": _any_tool_ready(tools, "gradle", "maven"),
        "path": _first_tool_path(tools, "gradle", "maven"),
    }
    tools["haskell_build"] = {
        "ready": _any_tool_ready(tools, "cabal", "stack"),
        "path": _first_tool_path(tools, "cabal", "stack"),
    }
    catalog = [
        {"id": "dotnet8", "name": ".NET SDK 8", "installed": 8 in dotnet, "installable": os.name == "nt"},
        {"id": "dotnet9", "name": ".NET SDK 9", "installed": 9 in dotnet, "installable": os.name == "nt"},
        {"id": "dotnet10", "name": ".NET SDK 10", "installed": 10 in dotnet, "installable": os.name == "nt"},
        {"id": "java8", "name": "Java/JDK 8 runtime and compiler", "installed": 8 in java, "installable": os.name == "nt"},
        {"id": "java11", "name": "Java/JDK 11 runtime and compiler", "installed": 11 in java, "installable": os.name == "nt"},
        {"id": "java17", "name": "Java/JDK 17 runtime and compiler", "installed": 17 in java, "installable": os.name == "nt"},
        {"id": "java21", "name": "Java/JDK 21 runtime and compiler", "installed": 21 in java, "installable": os.name == "nt"},
        {"id": "node20", "name": "Node.js 20 with npm (Angular 17–18)", "installed": 20 in installed_node_majors() and tools["npm"]["ready"], "installable": False},
        {"id": "node22", "name": "Node.js 22 with npm (Angular 19–22, React 16–19)", "installed": 22 in installed_node_majors() and tools["npm"]["ready"], "installable": False},
        {"id": "node24", "name": "Node.js 24 with npm (Angular 20-22)", "installed": 24 in installed_node_majors() and tools["npm"]["ready"], "installable": False},
        {"id": "postgresql16", "name": "PostgreSQL 16 Compose server and psql client", "installed": tools["database_clients"]["postgresql"]["ready"], "installable": False},
        {"id": "mysql_client", "name": "MySQL-compatible client (MariaDB CLI)", "installed": tools["database_clients"]["mysql"]["ready"], "installable": False},
        {"id": "sqlserver_client", "name": "SQL Server ODBC 18, sqlcmd and bcp clients", "installed": tools["database_clients"]["sql_server"]["ready"], "installable": False},
        {"id": "sqlite_client", "name": "SQLite 3 CLI", "installed": tools["database_clients"]["sqlite"]["ready"], "installable": False},
        {"id": "sql_dialects", "name": "SQL dialect parsers (including Oracle and Db2)", "installed": tools["sql_dialects"]["ready"], "installable": False},
        {"id": "maven", "name": "Apache Maven", "installed": tools["maven"]["ready"], "installable": False},
        {"id": "gradle", "name": "Gradle", "installed": tools["gradle"]["ready"], "installable": False},
        {"id": "python312", "name": "Python 3.12", "installed": tools["python"]["ready"], "installable": os.name == "nt"},
        {"id": "go", "name": "Go SDK", "installed": tools["go"]["ready"], "installable": os.name == "nt"},
        {"id": "php", "name": "PHP 8.3", "installed": tools["php"]["ready"], "installable": os.name == "nt"},
        {"id": "ruby", "name": "Ruby with DevKit", "installed": tools["ruby"]["ready"], "installable": os.name == "nt"},
        {"id": "llvm", "name": "LLVM C/C++ compiler", "installed": _all_tools_ready(tools, "c", "cpp"), "installable": os.name == "nt"},
        {"id": "cobol", "name": "GnuCOBOL", "installed": tools["cobol"]["ready"], "installable": False},
        {"id": "protoc", "name": "Protocol Buffers compiler", "installed": tools["protobuf"]["ready"], "installable": os.name == "nt"},
        {"id": "bash", "name": "Bash (Git for Windows)", "installed": tools["shell"]["ready"], "installable": os.name == "nt"},
        {"id": "rust", "name": "Rust (rustc)", "installed": tools["rust"]["ready"], "installable": os.name == "nt"},
        {"id": "swift", "name": "Swift toolchain", "installed": tools["swift"]["ready"], "installable": os.name == "nt"},
        {"id": "kotlin", "name": "Kotlin compiler", "installed": tools["kotlin"]["ready"], "installable": os.name == "nt"},
        {"id": "r", "name": "R", "installed": tools["r"]["ready"], "installable": os.name == "nt"},
        {"id": "scala", "name": "Scala compiler", "installed": tools["scala"]["ready"], "installable": os.name == "nt"},
        {"id": "haskell", "name": "Haskell (GHC via GHCup)", "installed": tools["haskell"]["ready"], "installable": os.name == "nt"},
        {"id": "elixir", "name": "Elixir", "installed": tools["elixir"]["ready"], "installable": os.name == "nt"},
        {"id": "dart", "name": "Dart SDK", "installed": tools["dart"]["ready"], "installable": os.name == "nt"},
        {"id": "flutter", "name": "Flutter SDK", "installed": tools["flutter"]["ready"], "installable": os.name == "nt"},
        {"id": "julia", "name": "Julia", "installed": tools["julia"]["ready"], "installable": os.name == "nt"},
        {"id": "fortran", "name": "Fortran (gfortran/flang)", "installed": tools["fortran"]["ready"], "installable": os.name == "nt"},
        {"id": "ada", "name": "Ada (GNAT)", "installed": tools["ada"]["ready"], "installable": os.name == "nt"},
        {"id": "pascal", "name": "Free Pascal", "installed": tools["pascal"]["ready"], "installable": os.name == "nt"},
        {"id": "erlang", "name": "Erlang/OTP", "installed": tools["erlang"]["ready"], "installable": os.name == "nt"},
        {"id": "ocaml", "name": "OCaml", "installed": tools["ocaml"]["ready"], "installable": os.name == "nt"},
        {"id": "prolog", "name": "SWI-Prolog", "installed": tools["prolog"]["ready"], "installable": os.name == "nt"},
        {"id": "lisp", "name": "Common Lisp (SBCL)", "installed": tools["lisp"]["ready"], "installable": os.name == "nt"},
        {"id": "clojure", "name": "Clojure CLI", "installed": tools["clojure"]["ready"], "installable": os.name == "nt"},
        {"id": "git", "name": "Git and GitHub-compatible CLI workflow", "installed": tools["git"]["ready"], "installable": os.name == "nt"},
    ]
    catalog.extend(
        {"id": f"angular{version}", "name": f"Angular {version} scaffold + compatible Node build runtime", "installed": _angular_node_runtime_available(version) and tools["npm"]["ready"], "installable": False}
        for version in range(17, 23)
    )
    catalog.extend(
        {"id": f"react{version}", "name": f"React {version} scaffold + Node 22 build runtime", "installed": 22 in installed_node_majors() and tools["npm"]["ready"], "installable": False}
        for version in range(16, 20)
    )
    required = (
        "dotnet", "node", "npm", "java", "jvm_build", "python", "go", "php",
        "ruby", "c", "cpp", "cobol", "typescript_validator", "sql_parser",
        "db2_sql_parser", "yaml_parser", "json_parser", "xml_parser",
        "toml_parser", "graphql_parser", "terraform", "protobuf", "shell",
        "jenkinsfile", "rust", "swift", "kotlin", "r", "scala", "haskell",
        "elixir", "dart", "julia", "fortran", "ada", "pascal", "erlang",
        "ocaml", "prolog", "lisp", "clojure", "abap", "rpg", "jcl", "mumps",
        "natural", "progress4gl", "apex", "pli", "sql_dialects", "database_clients",
    )
    return {"ready": all(tools[name]["ready"] for name in required),
            "tools": tools, "catalog": catalog}


# Function: _materialize
def _materialize(output: dict[str, str], tmp_dir: Path) -> None:
    root = tmp_dir.resolve()
    for rel_path, content in output.items():
        if not isinstance(content, str):
            raise TypeError(f"Generated artifact {rel_path!r} does not contain text")
        candidate = Path(str(rel_path))
        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
            raise ValueError(f"Generated artifact path is outside the project boundary: {rel_path!r}")
        file_path = (root / candidate).resolve()
        try:
            file_path.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"Generated artifact path is outside the project boundary: {rel_path!r}"
            ) from exc
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")


_WIN_DRIVE_WITH_LEADING_SLASH = re.compile(r"^/([A-Za-z]:)")


# Function: _rel_to_output_key
def _rel_to_output_key(reported_path: str, base_dir: Path, tmp_dir: Path) -> str:
    """Normalize a compiler-reported path (absolute or cwd-relative, native
    separators) back to the forward-slash-relative form used as keys in the
    `output` dict, so build errors can be attached to the right file.
    Maven-on-Windows reports absolute paths as "/C:/Users/..." (a leading
    slash before the drive letter) rather than "C:/Users/..." — strip that
    or Path() treats the whole thing as relative and relative_to() below
    always fails, silently degrading every Java error to a raw absolute path."""
    reported_path = _WIN_DRIVE_WITH_LEADING_SLASH.sub(r"\1", reported_path)
    p = Path(reported_path)
    p = p if p.is_absolute() else (base_dir / p)
    try:
        rel = p.resolve().relative_to(tmp_dir.resolve())
    except (ValueError, OSError):
        rel = Path(reported_path)
    return str(rel).replace("\\", "/")


# Function: _find_one
def _find_one(tmp_dir: Path, pattern: str) -> Path | None:
    ignored = {"node_modules", "bin", "obj", ".git", "dist", "target"}
    matches = [path for path in tmp_dir.rglob(pattern) if not ignored.intersection(path.relative_to(tmp_dir).parts)]
    matches.sort(key=lambda path: (len(path.relative_to(tmp_dir).parts), path.as_posix()))
    return matches[0] if matches else None


# ─── C# / .NET ──────────────────────────────────────────────────────────────

# Function: _parse_parenthesized_diagnostic
def _parse_parenthesized_diagnostic(line: str, code_prefix: str) -> tuple[str, str, str, str] | None:
    """Parse path(line,col): error CODE: message without backtracking."""
    location, separator, detail = line.partition("): error ")
    if not separator:
        return None
    file_path, open_paren, coordinates = location.rpartition("(")
    if not open_paren:
        return None
    line_no, comma, column = coordinates.partition(",")
    code, colon, message = detail.partition(":")
    if not (comma and colon and line_no.isdigit() and column.isdigit() and code.startswith(code_prefix)):
        return None
    cleaned_message = message.strip()
    if code_prefix == "CS" and cleaned_message.endswith("]") and " [" in cleaned_message:
        cleaned_message = cleaned_message.rsplit(" [", 1)[0]
    return file_path, line_no, code, cleaned_message


# Function: _dotnet_solution_integrity_error
def _dotnet_solution_integrity_error(solution: Path, build_root: Path) -> str | None:
    """Return an actionable structural error before NuGet sees the solution."""
    try:
        content = solution.read_text(encoding="utf-8-sig")
    except OSError as exc:
        return f"Unable to read generated .NET solution {solution.name}: {exc}"

    references = re.findall(
        r'^Project\("\{[^}]+\}"\)\s*=\s*"[^"]+",\s*"([^"]+\.csproj)"',
        content,
        re.MULTILINE | re.IGNORECASE,
    )
    if not references:
        return (
            f"Generated .NET solution {solution.name} contains no .csproj entries; "
            "solution generation must include at least one real MSBuild project."
        )
    if "GlobalSection(SolutionConfigurationPlatforms)" not in content or (
        "GlobalSection(ProjectConfigurationPlatforms)" not in content
    ):
        return (
            f"Generated .NET solution {solution.name} is missing required solution/project "
            "configuration sections."
        )

    root = build_root.resolve()
    missing = []
    invalid = []
    for reference in references:
        candidate = (solution.parent / reference.replace("\\", "/")).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            invalid.append(reference)
            continue
        if not candidate.is_file():
            missing.append(reference)
    if invalid:
        return f"Generated .NET solution {solution.name} has out-of-root project path(s): {', '.join(invalid)}"
    if missing:
        return f"Generated .NET solution {solution.name} references missing project(s): {', '.join(missing)}"
    return None


# Function: _run_dotnet_build
def _run_dotnet_build(tmp_dir: Path) -> BuildResult:
    if not _DOTNET_PATH:
        return BuildResult(False, _MISSING_TOOLCHAIN, {_BUILD_KEY: ["dotnet not found on PATH"]})

    sln = _find_one(tmp_dir, "*.sln")
    target = sln if sln else _find_one(tmp_dir, "*.csproj")
    if not target:
        return BuildResult(False, _MISSING_MANIFEST, {_BUILD_KEY: ["Generated .NET output has no .sln or .csproj"]})
    if sln:
        integrity_error = _dotnet_solution_integrity_error(sln, tmp_dir)
        if integrity_error:
            return BuildResult(False, "dotnet-solution", {_BUILD_KEY: [integrity_error]})

    try:
        proc = subprocess.run(
            [_DOTNET_PATH, "build", str(target), "--nologo", "-v", "quiet", "-warnaserror"],
            capture_output=True, text=True, timeout=_BUILD_TIMEOUT, cwd=str(target.parent),
        )
    except subprocess.TimeoutExpired as exc:
        return BuildResult(False, "dotnet", {_BUILD_KEY: [f"dotnet build timed out after {_BUILD_TIMEOUT}s"]}, str(exc))

    combined = proc.stdout + "\n" + proc.stderr
    if proc.returncode == 0:
        try:
            test_proc = subprocess.run(
                [_DOTNET_PATH, "test", str(target), "--no-build", "--nologo", "-v", "quiet"],
                capture_output=True, text=True, timeout=_BUILD_TIMEOUT, cwd=str(target.parent),
            )
        except subprocess.TimeoutExpired as exc:
            return BuildResult(
                False, "dotnet-build+test",
                {"<tests>": [f"dotnet test timed out after {_BUILD_TIMEOUT}s"]},
                combined + "\n" + str(exc),
            )
        test_output = test_proc.stdout + "\n" + test_proc.stderr
        if test_proc.returncode == 0:
            return BuildResult(True, "dotnet-build+test", raw_output=combined + "\n" + test_output)
        return BuildResult(
            False, "dotnet-build+test",
            {"<tests>": [test_output.strip()[-2000:] or "dotnet test failed with no diagnostic output"]},
            combined + "\n" + test_output,
        )

    errors_by_file: dict[str, list[str]] = {}
    for line in combined.splitlines():
        parsed = _parse_parenthesized_diagnostic(line.strip(), "CS")
        if not parsed:
            continue
        file_path, _lineno, code, message = parsed
        key = _rel_to_output_key(file_path, target.parent, tmp_dir)
        errors_by_file.setdefault(key, []).append(f"{code}: {message}")

    if not errors_by_file:
        # Build failed (non-zero exit) but no per-file line matched — e.g. a
        # restore failure. Attach the raw tail so the repair loop has SOMETHING.
        errors_by_file[_BUILD_KEY] = [combined.strip()[-1500:] or "dotnet build failed with no parseable output"]

    for key, messages in list(errors_by_file.items()):
        errors_by_file[key] = list(dict.fromkeys(messages))
    return BuildResult(False, "dotnet", errors_by_file, combined)


# ─── Java / Maven ───────────────────────────────────────────────────────────

# Function: _parse_maven_diagnostic
def _parse_maven_diagnostic(line: str) -> tuple[str, str] | None:
    """Parse Maven's [ERROR] path.java:[line,col] message format."""
    prefix = "[ERROR] "
    if not line.startswith(prefix):
        return None
    location, separator, message = line[len(prefix):].partition("] ")
    file_path, marker, coordinates = location.rpartition(":[")
    if not (separator and marker and file_path.endswith(".java")):
        return None
    line_no, comma, column = coordinates.partition(",")
    if not (comma and line_no.isdigit() and column.isdigit()):
        return None
    return file_path, message.strip()


# Function: _parse_maven_project_diagnostic
def _parse_maven_project_diagnostic(line: str) -> tuple[str, str] | None:
    """Attach a missing-reactor-module error to its parent POM."""
    normalized = line.casefold()
    child_marker = "child module "
    missing_marker = " does not exist"
    start = normalized.find(child_marker)
    end = normalized.find(missing_marker, start + len(child_marker))
    if start < 0 or end < 0:
        return None
    declaration = line[start + len(child_marker):end].strip()
    separator_index = declaration.casefold().rfind(" of ")
    if separator_index < 0:
        return None
    child = declaration[:separator_index]
    parent = declaration[separator_index + len(" of "):]
    if not parent.casefold().endswith(_POM_XML):
        return None
    return parent.strip(), f"Declared Maven child module does not exist: {child.strip()}"


def _project_java_major(manifest: Path) -> int | None:
    try:
        content = manifest.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    if manifest.name.casefold() == _POM_XML:
        patterns = (
            r"<maven\.compiler\.release>\s*(\d+)",
            r"<java\.version>\s*(\d+)",
            r"<maven\.compiler\.source>\s*(\d+)",
            r"<maven\.compiler\.target>\s*(\d+)",
        )
    else:
        patterns = (
            r"(?:JavaLanguageVersion\.of|jvmToolchain)\s*\(?\s*(\d+)",
            r"(?:sourceCompatibility|targetCompatibility)\s*=\s*['\"]?(\d+)",
            r"JavaVersion\.VERSION_(\d+)",
        )
    for pattern in patterns:
        match = re.search(pattern, content, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _java_build_environment(manifest: Path | None = None) -> dict:
    build_env = os.environ.copy()
    requested = _project_java_major(manifest) if manifest else None
    java_home = _java_home_for_major(requested) if requested else None
    java_home = java_home or _preferred_java_home()
    if java_home:
        build_env["JAVA_HOME"] = str(java_home)
        build_env["PATH"] = str(java_home / "bin") + os.pathsep + build_env.get("PATH", "")
    return build_env


def _append_maven_detail(
    line: str, last_java_key: str | None, errors_by_file: dict[str, list[str]],
) -> None:
    prefix = "[error]"
    normalized = line.casefold()
    if not normalized.startswith(prefix) or not last_java_key or not errors_by_file.get(last_java_key):
        return
    label, separator, detail = line[len(prefix):].strip().partition(":")
    if separator and label.casefold() in {
        "symbol", "location", "required", "found", "reason",
    }:
        errors_by_file[last_java_key][-1] += f" — {label.casefold()}: {detail.strip()}"


def _collect_maven_errors(combined: str, pom: Path, tmp_dir: Path) -> dict[str, list[str]]:
    errors_by_file: dict[str, list[str]] = {}
    last_java_key: str | None = None
    for line in combined.splitlines():
        stripped = line.strip()
        parsed = _parse_maven_diagnostic(stripped)
        project_parsed = _parse_maven_project_diagnostic(stripped)
        diagnostic = parsed or project_parsed
        if not diagnostic:
            _append_maven_detail(stripped, last_java_key, errors_by_file)
            continue
        file_path, message = diagnostic
        key = _rel_to_output_key(file_path, pom.parent, tmp_dir)
        errors_by_file.setdefault(key, []).append(message)
        last_java_key = key if parsed else None
    return errors_by_file


# Function: _run_maven_build
def _run_maven_build(tmp_dir: Path) -> BuildResult:
    if not _MVN_PATH:
        return BuildResult(False, _MISSING_TOOLCHAIN, {_BUILD_KEY: ["mvn not found on PATH"]})

    pom = _find_one(tmp_dir, _POM_XML)
    if not pom:
        return BuildResult(False, _MISSING_MANIFEST, {_BUILD_KEY: ["Generated Java output has no pom.xml"]})

    def run(repository: Path):
        repository.mkdir(parents=True, exist_ok=True)
        return subprocess.run(
            [
                _MVN_PATH,
                "-B",
                "-q",
                "-U",
                "--fail-at-end",
                f"-Dmaven.repo.local={repository}",
                "verify",
            ],
            capture_output=True, text=True, timeout=_BUILD_TIMEOUT, cwd=str(pom.parent),
            env=_java_build_environment(pom),
        )

    try:
        proc = run(_MAVEN_REPOSITORY)
        combined = proc.stdout + "\n" + proc.stderr
        if proc.returncode and any(signal in combined.casefold() for signal in _MAVEN_REPOSITORY_FAILURES):
            # A locked/corrupt shared dependency is an infrastructure fault,
            # not generated-code evidence. Retry once in an isolated repository
            # without deleting or mutating the shared cache used by other jobs.
            proc = run(tmp_dir / ".maven-repository-retry")
    except subprocess.TimeoutExpired as exc:
        return BuildResult(False, "maven", {_BUILD_KEY: [f"mvn verify timed out after {_BUILD_TIMEOUT}s"]}, str(exc))

    combined = proc.stdout + "\n" + proc.stderr
    if proc.returncode == 0:
        return BuildResult(True, "maven", raw_output=combined)

    errors_by_file = _collect_maven_errors(combined, pom, tmp_dir)

    if not errors_by_file:
        errors_by_file[_BUILD_KEY] = [combined.strip()[-1500:] or "mvn verify failed with no parseable output"]

    return BuildResult(False, "maven", errors_by_file, combined)


# Function: _run_java_project_build
def _run_java_project_build(tmp_dir: Path) -> BuildResult:
    """Run a real Java project build based on whichever build manifest exists.

    Dynamic route selection avoids hard-coding Maven-only readiness for stacks
    whose generated projects can be Gradle-based (for example Quarkus/Micronaut
    variants), while keeping strict compile validation fail-closed.
    """
    pom = _find_one(tmp_dir, _POM_XML)
    gradle = _find_one(tmp_dir, "build.gradle") or _find_one(tmp_dir, "build.gradle.kts")

    if pom:
        return _run_maven_build(tmp_dir)
    if gradle:
        return _run_manifest_build(tmp_dir, "gradle", ["test", "--no-daemon"])

    if _command_usable("mvn"):
        return BuildResult(False, _MISSING_MANIFEST, {_BUILD_KEY: ["Generated Java output has no pom.xml/build.gradle/build.gradle.kts"]})
    if _command_usable("gradle"):
        return BuildResult(False, _MISSING_MANIFEST, {_BUILD_KEY: ["Generated Java output has no build.gradle/build.gradle.kts/pom.xml"]})
    return BuildResult(False, _MISSING_TOOLCHAIN, {_BUILD_KEY: ["Neither mvn nor gradle is available on PATH"]})


# ─── TypeScript / npm ───────────────────────────────────────────────────────

# Function: _parse_angular_diagnostic
def _parse_angular_diagnostic(line: str) -> tuple[str, str, str] | None:
    """Parse Angular path:line:col - error TS/NGxxxx: message output."""
    normalized = line.removeprefix("Error: ")
    location, separator, detail = normalized.partition(" - error ")
    if not separator:
        return None
    file_path, colon, column = location.rpartition(":")
    file_path, line_colon, line_no = file_path.rpartition(":")
    code, message_colon, message = detail.partition(":")
    if not (
        colon and line_colon and message_colon
        and line_no.isdigit() and column.isdigit()
        and file_path.endswith((".ts", ".tsx"))
        and code.startswith(("TS", "NG"))
    ):
        return None
    return file_path, code, message.strip()


# Function: _parse_typescript_diagnostic
def _parse_typescript_diagnostic(line: str) -> tuple[str, str, str] | None:
    parsed = _parse_parenthesized_diagnostic(line, "TS")
    if parsed:
        file_path, _line_no, code, message = parsed
        return file_path, code, message
    return _parse_angular_diagnostic(line)


# Function: _run_npm_tsc_build
def _load_package_data(package_path: Path) -> dict:
    try:
        return json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# Function: _is_transient_toolchain_crash
def _is_transient_toolchain_crash(proc: subprocess.CompletedProcess[str]) -> bool:
    """True when npm/node crashed itself (allocator/host memory pressure)
    rather than reporting a real problem with the generated project. These
    crashes are non-deterministic — the exact same package.json succeeds on
    the very next attempt once transient pressure clears — so failing the
    whole build (and the release quality gate) on the first occurrence
    reports a false "generated code is broken" verdict."""
    if proc.returncode == 0:
        return False
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".casefold()
    return any(signature in combined for signature in _TRANSIENT_TOOLCHAIN_SIGNATURES)


# Function: _run_npm_subprocess_with_retry
def _run_npm_subprocess_with_retry(
    command: list, project_dir: Path, timeout: int, timeout_key: str, timeout_message: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | BuildResult:
    """Run an npm/npx subprocess, retrying only on a detected transient
    toolchain crash (see _is_transient_toolchain_crash) — never on a normal
    non-zero exit from a real dependency/compile problem."""
    attempt = 0
    last_proc: subprocess.CompletedProcess[str] | None = None
    while attempt <= _TOOLCHAIN_CRASH_RETRIES:
        attempt += 1
        try:
            proc = _run_process_tree(
                command, timeout=timeout, cwd=str(project_dir), env=env,
            )
        except subprocess.TimeoutExpired as exc:
            return BuildResult(False, _NPM_TSC, {timeout_key: [timeout_message]}, str(exc))
        if not _is_transient_toolchain_crash(proc) or attempt > _TOOLCHAIN_CRASH_RETRIES:
            return proc
        last_proc = proc
        time.sleep(_TOOLCHAIN_CRASH_BACKOFF)
    return last_proc


# Function: _npm_install
def _npm_install(project_dir: Path) -> subprocess.CompletedProcess[str] | BuildResult:
    npm, _npx, environment = _npm_runtime(project_dir / _PACKAGE_JSON)
    if not npm:
        return BuildResult(False, _MISSING_TOOLCHAIN, {_BUILD_KEY: ["npm is missing for this project's selected Node runtime"]})
    return _run_npm_subprocess_with_retry(
        [npm, "install", "--no-fund", "--no-audit"], project_dir, _NPM_INSTALL_TIMEOUT,
        _INSTALL_KEY, f"npm install timed out after {_NPM_INSTALL_TIMEOUT}s",
        env=environment,
    )


# Function: _npm_compile
def _npm_compile(project_dir: Path, build_script: str) -> subprocess.CompletedProcess[str] | BuildResult:
    npm, npx, environment = _npm_runtime(project_dir / _PACKAGE_JSON)
    command = [npm, "run", "build"] if build_script else [npx, "tsc", "--noEmit"]
    return _run_npm_subprocess_with_retry(
        command, project_dir, _NPM_BUILD_TIMEOUT,
        _BUILD_KEY, f"frontend build timed out after {_NPM_BUILD_TIMEOUT}s",
        env=environment,
    )


def _npm_test(project_dir: Path) -> subprocess.CompletedProcess[str] | BuildResult:
    npm, _npx, environment = _npm_runtime(project_dir / _PACKAGE_JSON)
    return _run_npm_subprocess_with_retry(
        [npm, "run", "test"], project_dir, _NPM_BUILD_TIMEOUT,
        "<tests>", f"frontend tests timed out after {_NPM_BUILD_TIMEOUT}s",
        env=environment,
    )


# Function: _typescript_errors
def _typescript_errors(output: str, project_dir: Path, tmp_dir: Path) -> dict[str, list[str]]:
    errors: dict[str, list[str]] = {}
    for line in output.splitlines():
        parsed = _parse_typescript_diagnostic(line.strip())
        if not parsed:
            continue
        file_path, code, message = parsed
        key = _rel_to_output_key(file_path, project_dir, tmp_dir)
        if "/node_modules/" in f"/{key}":
            key = _DEPENDENCY_COMPATIBILITY_KEY
        errors.setdefault(key, []).append(f"{code}: {message}")
    return {key: list(dict.fromkeys(messages))[:20] for key, messages in errors.items()}


# Function: _vite_manifest_errors
def _vite_manifest_errors(output: str, package_path: Path, tmp_dir: Path) -> dict[str, list[str]]:
    """Map missing packages to package.json and local imports to their importer."""
    errors: dict[str, list[str]] = {}
    detailed = re.compile(
        r"""(?:failed to resolve import|could not resolve)\s+["']([^"']+)["']\s+from\s+["']([^"']+)["']""",
        re.IGNORECASE,
    )
    seen = set()
    for match in detailed.finditer(output):
        specifier, importer = match.groups()
        seen.add(specifier)
        is_local = specifier.startswith((".", "/", "@/", "src/", "~/"))
        target = importer if is_local else str(package_path)
        key = _rel_to_output_key(target, package_path.parent, tmp_dir)
        category = "Local frontend import" if is_local else "Frontend dependency"
        errors.setdefault(key, []).append(f"{category} is not resolvable: {specifier}")
    for pattern in (
        r"""failed to resolve import\s+["']([^"']+)["']""",
        r"""could not resolve\s+["']([^"']+)["']""",
    ):
        for match in re.finditer(pattern, output, re.IGNORECASE):
            specifier = match.group(1)
            if specifier in seen or specifier.startswith((".", "/", "@/", "src/", "~/")):
                continue
            key = _rel_to_output_key(str(package_path), package_path.parent, tmp_dir)
            errors.setdefault(key, []).append(
                f"Frontend dependency is imported but not resolvable: {specifier}"
            )
    return {
        key: list(dict.fromkeys(messages))[:20]
        for key, messages in errors.items()
    }


# Function: _run_npm_tsc_build
def _run_npm_tsc_build(
    tmp_dir: Path,
    package_path: Path | None = None,
    manifest_diagnostics: bool = False,
) -> BuildResult:
    if not (_NPM_PATH and _NPX_PATH):
        return BuildResult(False, _MISSING_TOOLCHAIN, {_BUILD_KEY: ["npm/npx not found on PATH"]})

    pkg = package_path or _find_one(tmp_dir, _PACKAGE_JSON)
    if not pkg:
        return BuildResult(
            False, _MISSING_MANIFEST,
            {_BUILD_KEY: [f"Generated Node/TypeScript output has no {_PACKAGE_JSON}"]},
        )
    project_dir = pkg.parent
    package_data = _load_package_data(pkg)
    install = _npm_install(project_dir)
    if isinstance(install, BuildResult):
        return install
    if install.returncode != 0:
        combined = install.stdout + "\n" + install.stderr
        return BuildResult(False, _NPM_TSC, {_INSTALL_KEY: [combined.strip()[-1500:]]}, combined)

    build_script = (package_data.get("scripts") or {}).get("build")
    proc = _npm_compile(project_dir, build_script)
    if isinstance(proc, BuildResult):
        return proc

    combined = install.stdout + proc.stdout + "\n" + proc.stderr
    checker = "npm-build" if build_script else _NPM_TSC
    if proc.returncode == 0:
        test_script = (package_data.get("scripts") or {}).get("test")
        if not test_script:
            return BuildResult(True, checker, raw_output=combined)
        test_proc = _npm_test(project_dir)
        if isinstance(test_proc, BuildResult):
            return test_proc
        test_output = test_proc.stdout + "\n" + test_proc.stderr
        if test_proc.returncode == 0:
            return BuildResult(True, checker + "+npm-test", raw_output=combined + "\n" + test_output)
        return BuildResult(
            False, checker + "+npm-test",
            {"<tests>": [test_output.strip()[-2000:] or "npm test failed with no diagnostic output"]},
            combined + "\n" + test_output,
        )

    # Unlike validators.py's per-file check (which filters TSxxxx >= 2000 as
    # dependency-resolution noise since no node_modules exist there), a real
    # `npm install` just ran here — TS2xxx type/module-resolution errors are
    # genuine signal now, not noise. Keep every error-level diagnostic.
    errors_by_file = _typescript_errors(proc.stdout + "\n" + proc.stderr, project_dir, tmp_dir)
    if not errors_by_file and manifest_diagnostics:
        errors_by_file = _vite_manifest_errors(
            proc.stdout + "\n" + proc.stderr, pkg, tmp_dir,
        )
    if not errors_by_file:
        errors_by_file[_BUILD_KEY] = [combined.strip()[-1500:] or "tsc failed with no parseable output"]
    return BuildResult(False, checker, errors_by_file, combined)


# Function: _run_all_npm_builds
def _run_all_npm_builds(tmp_dir: Path) -> BuildResult:
    """Restore and compile every generated Node workspace independently."""
    packages = sorted(
        path for path in tmp_dir.rglob(_PACKAGE_JSON)
        if "node_modules" not in path.parts
    )
    if not packages:
        return BuildResult(False, _MISSING_MANIFEST, {_BUILD_KEY: ["Generated output has no package.json"]})
    result: BuildResult | None = None
    for package in packages:
        current = _run_npm_tsc_build(tmp_dir, package)
        result = current if result is None else _combine_build_results(result, current)
    return result or BuildResult(False, _MISSING_MANIFEST)


# Function: _run_c_family_build
def _compile_c_family_sources(
    tmp_dir: Path, language: str, executable: str, standard_flag: str, files: list[Path],
) -> tuple[list[Path], list[str], BuildResult | None]:
    output_parts: list[str] = []
    objects: list[Path] = []
    for source in files:
        obj_path = source.with_suffix(".o")
        try:
            proc = subprocess.run(
                [executable, standard_flag, "-c", *native_include_args(), str(source), "-o", str(obj_path)],
                cwd=str(tmp_dir), capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return objects, output_parts, BuildResult(
                False, f"{language}-build", {_BUILD_KEY: [str(exc)]},
            )
        combined = (proc.stdout + "\n" + proc.stderr).strip()
        output_parts.append(combined)
        if proc.returncode != 0:
            rel = source.relative_to(tmp_dir).as_posix()
            return objects, output_parts, BuildResult(
                False, f"{language}-build",
                {_BUILD_KEY: [combined[-3000:] or f"Compilation failed for {rel}"]},
                "\n".join(output_parts),
            )
        objects.append(obj_path)
    return objects, output_parts, None


def _c_family_link_target(tmp_dir: Path, files: list[Path]) -> tuple[list[str], Path]:
    combined_source = "\n".join(
        source.read_text(encoding="utf-8", errors="ignore") for source in files
    )
    has_entry_point = bool(re.search(r"\b(?:int|void)\s+main\s*\(|\bwWinMain\b|\bWinMain\b", combined_source))
    if has_entry_point:
        return [], tmp_dir / ("__modernization_link_check.exe" if os.name == "nt" else "__modernization_link_check")
    return ["-shared"], tmp_dir / ("__modernization_link_check.dll" if os.name == "nt" else "__modernization_link_check.so")


def _link_c_family_objects(
    tmp_dir: Path, language: str, executable: str, standard_flag: str,
    objects: list[Path], link_flags: list[str], link_output: Path, output_parts: list[str],
) -> BuildResult:
    try:
        link_proc = subprocess.run(
            [executable, standard_flag, *link_flags, *map(str, objects), "-o", str(link_output)],
            cwd=str(tmp_dir), capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return BuildResult(
            False, f"{language}-build", {_BUILD_KEY: [str(exc)]}, "\n".join(output_parts),
        )
    link_combined = (link_proc.stdout + "\n" + link_proc.stderr).strip()
    output_parts.append(link_combined)
    if link_proc.returncode != 0:
        return BuildResult(
            False, f"{language}-build",
            {_BUILD_KEY: [link_combined[-3000:] or "Link failed"]},
            "\n".join(output_parts),
        )
    return BuildResult(True, f"{language}-build", raw_output="\n".join(output_parts))


def _run_c_family_build(tmp_dir: Path, language: str) -> BuildResult:
    """Real project build for C/C++: compile every source file to an object
    file, then link them together. A per-file -fsyntax-only pass (the old
    route, still used by validators.py's Phase-1 per-file check) treats every
    translation unit in isolation and can never catch cross-file link errors -
    an undefined reference to a sibling file's function, or the same symbol
    defined twice. Linking is the only way to catch those, so this is what
    makes c/cpp genuinely "project ready" rather than single-file-only."""
    files = _source_files(tmp_dir, language)
    if not files:
        return BuildResult(
            False, "missing-source",
            {_BUILD_KEY: [f"Generated output contains no {language} source files"]},
        )

    executable = _source_check_executable(language, "")
    if not executable:
        return BuildResult(
            False, _MISSING_TOOLCHAIN,
            {_BUILD_KEY: [f"Required {'C' if language == 'c' else 'C++'} compiler is not installed"]},
        )

    standard_flag = "-std=c17" if language == "c" else "-std=c++23"
    objects, output_parts, compile_failure = _compile_c_family_sources(
        tmp_dir, language, executable, standard_flag, files,
    )
    if compile_failure:
        return compile_failure

    # Decide executable vs. shared-library link mode from the source itself rather
    # than pattern-matching linker error text after the fact: lld-link (Windows)
    # and GNU ld report a missing entry point completely differently ("subsystem
    # must be defined" vs. "undefined reference to `main'"), so guessing from the
    # error message is fragile across linkers/versions. A library with no `main`
    # is valid, not a defect - detect that case up front instead.
    link_flags, link_output = _c_family_link_target(tmp_dir, files)
    return _link_c_family_objects(
        tmp_dir, language, executable, standard_flag,
        objects, link_flags, link_output, output_parts,
    )


# ─── Public API ─────────────────────────────────────────────────────────────

_CONFIGURED_MANIFEST_ROUTES = {
    "kotlin": ("gradle", ["test", "--no-daemon"], None),
    "swift": ("swift", ["test"], None),
    "scala": ("sbt", ["-batch", "test"], None),
    "elixir": ("mix", ["deps.get"], ["test"]),
    "r": ("Rscript", ["-e", "parse(file='app.R')"], None),
    "julia": ("julia", ["--project=.", "-e", "using Pkg; Pkg.instantiate(); Pkg.test()"], None),
    "lisp": ("sbcl", ["--non-interactive", "--load", "main.lisp"], None),
    "shell": ("bash", ["smoke.sh"], None),
}


def _run_backend_and_frontend(
    tmp_dir: Path, primary: BuildResult, manifest_diagnostics: bool = False,
) -> BuildResult:
    frontend = (
        _run_npm_tsc_build(tmp_dir, manifest_diagnostics=manifest_diagnostics)
        if _find_one(tmp_dir, _PACKAGE_JSON) else None
    )
    return _combine_build_results(primary, frontend)


def _run_core_language_build(tmp_dir: Path, language: str) -> BuildResult:
    if language == "csharp":
        return _run_backend_and_frontend(tmp_dir, _run_dotnet_build(tmp_dir))
    if language == "java":
        return _run_backend_and_frontend(
            tmp_dir, _run_java_project_build(tmp_dir), manifest_diagnostics=True,
        )
    if language == "typescript":
        return _run_all_npm_builds(tmp_dir)
    if language == "javascript":
        return _run_all_npm_builds(tmp_dir) if _find_one(tmp_dir, _PACKAGE_JSON) else _run_source_checks(tmp_dir, language)
    return _run_c_family_build(tmp_dir, language)


def _run_source_language_build(tmp_dir: Path, language: str) -> BuildResult:
    if language == "php" and _find_one(tmp_dir, "composer.json"):
        primary = _run_manifest_build(
            tmp_dir, "composer", ["install", "--no-interaction", "--prefer-dist"], ["test"],
        )
        return _run_backend_and_frontend(tmp_dir, primary)
    if language == "ruby" and _find_one(tmp_dir, "Gemfile"):
        primary = _run_manifest_build(
            tmp_dir, "bundle", ["install"], ["exec", "rails", "test"],
        )
        return _run_backend_and_frontend(tmp_dir, primary)
    if language == "cobol":
        return _run_cobol_project_build(tmp_dir)
    return _run_source_checks(tmp_dir, language)


def _run_sql_project_build(tmp_dir: Path, dialect_hint: str = "") -> BuildResult:
    """Parse every SQL artifact as one fail-closed project validation route."""
    from services.validators import validate_file
    files = sorted(tmp_dir.rglob("*.sql"))
    if not files:
        return BuildResult(False, "missing-source", {_BUILD_KEY: ["Generated SQL output has no .sql files"]})
    errors: dict[str, list[str]] = {}
    for path in files:
        relative = path.relative_to(tmp_dir).as_posix()
        result = validate_file(
            relative,
            path.read_text(encoding="utf-8"),
            "sql",
            tmp_dir,
            dialect_hint=dialect_hint,
        )
        if not result.passed:
            errors[relative] = list(result.diagnostics or ["SQL parser validation failed"])
    return BuildResult(not errors, "sql-project-parser", errors)


def _run_artifact_project_build(tmp_dir: Path, language: str) -> BuildResult:
    """Validate every generated configuration/deployment artifact with its parser."""
    from services.validators import validate_file
    files = [path for path in tmp_dir.rglob("*") if path.is_file() and ".modernization-venv" not in path.parts]
    if not files:
        return BuildResult(False, "missing-artifact", {_BUILD_KEY: ["Generated artifact project is empty"]})
    expected = {
        "yaml": lambda path: path.suffix.casefold() in {".yaml", ".yml"},
        "json": lambda path: path.suffix.casefold() == ".json",
        "toml": lambda path: path.suffix.casefold() == ".toml",
        "xml": lambda path: path.suffix.casefold() == ".xml",
        "graphql": lambda path: path.suffix.casefold() in {".graphql", ".gql", ".graphqls"},
        "dockerfile": lambda path: path.name.casefold().startswith("dockerfile"),
        "cloudformation": lambda path: path.suffix.casefold() in {".yaml", ".yml", ".json"},
        "kubernetes": lambda path: path.suffix.casefold() in {".yaml", ".yml"},
        "helm": lambda path: path.suffix.casefold() in {".yaml", ".yml", ".tpl"},
        "ansible": lambda path: path.suffix.casefold() in {".yaml", ".yml"},
        "github_actions": lambda path: path.suffix.casefold() in {".yaml", ".yml"},
        "markdown": lambda path: path.suffix.casefold() in {".md", ".markdown"},
        "hcl": lambda path: path.suffix.casefold() in {".tf", ".tfvars", ".hcl"},
        "protobuf": lambda path: path.suffix.casefold() == ".proto",
        "jenkinsfile": lambda path: path.name.casefold() == "jenkinsfile",
    }.get(language)
    if expected and not any(expected(path) for path in files):
        return BuildResult(
            False, "missing-artifact",
            {_BUILD_KEY: [f"Generated output contains no {language} artifact"]},
        )
    errors: dict[str, list[str]] = {}
    checked = 0
    for path in files:
        relative = path.relative_to(tmp_dir).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        result = validate_file(relative, content, language, tmp_dir)
        checked += 1
        if not result.passed:
            errors[relative] = list(result.diagnostics or ["Artifact parser validation failed"])
    if not checked:
        return BuildResult(False, "missing-artifact", {_BUILD_KEY: ["No text artifacts could be validated"]})
    return BuildResult(not errors, f"{language}-project-parser", errors)


def _run_dart_build(tmp_dir: Path) -> BuildResult:
    pubspec = _find_one(tmp_dir, _PUBSPEC_YAML)
    is_flutter = bool(pubspec and re.search(r"(?m)^\s*flutter\s*:", pubspec.read_text(encoding="utf-8")))
    if not is_flutter:
        return _run_manifest_build(tmp_dir, "dart", ["pub", "get"], ["test"])
    primary = _run_manifest_build(tmp_dir, "flutter", ["test"])
    dotnet = _run_dotnet_build(tmp_dir) if _find_one(tmp_dir, "*.csproj") else None
    return _combine_build_results(primary, dotnet)


def _run_special_language_build(tmp_dir: Path, language: str) -> BuildResult:
    if language == "clojure":
        return _run_maven_build(tmp_dir)
    if language == "dart":
        return _run_dart_build(tmp_dir)
    if language == "erlang":
        return _run_erlang_otp_build(tmp_dir)
    command = "stack" if _which("stack") else "cabal"
    stack_args = ["--skip-msys", "build", "--test"] if os.name == "nt" else ["build", "--test"]
    return _run_manifest_build(
        tmp_dir, command, stack_args if command == "stack" else ["build", "all"],
    )


def _run_swift_build(tmp_dir: Path) -> BuildResult:
    """Compile Vapor with Linux Swift on Windows, where Vapor 4 is POSIX-only.

    Swift itself supports Windows, but Vapor's stable AsyncHTTPClient dependency
    unconditionally compiles POSIX time shims. A native Windows compile therefore
    fails inside third-party C source before generated application code is reached.
    Docker provides the supported Linux compiler boundary used for deployment.
    """
    manifest = _find_one(tmp_dir, "Package.swift")
    if not manifest:
        return BuildResult(
            False, _MISSING_MANIFEST,
            {_BUILD_KEY: ["Generated output has no Package.swift"]},
        )
    docker = _which("docker") if os.name == "nt" else None
    if not docker:
        return _run_manifest_build(tmp_dir, "swift", ["test"])
    mount = f"type=bind,source={manifest.parent.resolve()},target=/workspace"
    try:
        proc = _run_process_tree(
            [
                docker, "run", "--rm", "--mount", mount,
                "-w", "/workspace", "swift:6.2-noble", "swift", "test",
            ],
            cwd=str(manifest.parent), timeout=_CONTAINER_BUILD_TIMEOUT,
            env=executable_environment(docker),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return BuildResult(False, "swift-docker-build", {_BUILD_KEY: [str(exc)]})
    combined = (proc.stdout + "\n" + proc.stderr).strip()
    if proc.returncode:
        diagnostic_lines = [
            line for line in combined.splitlines()
            if re.search(r"(?i)(?:\berror:|fatal(?:error)?:|undefined reference)", line)
        ]
        detail = "\n".join(diagnostic_lines[-40:] + [combined[-3000:]])[-12000:]
        return BuildResult(
            False, "swift-docker-build", {_BUILD_KEY: [detail]}, combined,
        )
    return BuildResult(True, "swift-docker-build", raw_output=combined)


def _dispatch_build(
    tmp_dir: Path, language: str, *, sql_dialect: str = "",
) -> BuildResult:
    if language in {"csharp", "java", "typescript", "javascript", "c", "cpp"}:
        return _run_core_language_build(tmp_dir, language)
    if language in {"python", "cobol", "php", "ruby", "go"}:
        return _run_source_language_build(tmp_dir, language)
    if language == "rust":
        return _run_backend_and_frontend(
            tmp_dir, _run_manifest_build(tmp_dir, "cargo", ["test", "--all-targets"]),
        )
    if language == "sql":
        return _run_sql_project_build(tmp_dir, sql_dialect)
    if language == "swift":
        return _run_swift_build(tmp_dir)
    if language in ARTIFACT_BUILD_LANGUAGES:
        return _run_artifact_project_build(tmp_dir, language)
    if language in _CONFIGURED_MANIFEST_ROUTES:
        tool, args, after = _CONFIGURED_MANIFEST_ROUTES[language]
        return _run_manifest_build(tmp_dir, tool, args, after)
    if language in {"clojure", "dart", "erlang", "haskell"}:
        return _run_special_language_build(tmp_dir, language)
    return BuildResult(
        False, "unsupported-build-route",
        {_BUILD_KEY: [f"No strict project validation route is registered for language={language!r}"]},
    )

# Function: run_build
def run_build(
    output: dict[str, str], language: str, tmp_dir: Path, *, sql_dialect: str = "",
) -> BuildResult:
    """Materialize `output` under tmp_dir and run a real build for the given
    language. Returns checker="skipped" for languages with no real build
    concept (python/sql — already covered by validators.py's syntax checks)
    or when the required toolchain isn't installed. Never raises."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _refresh_tool_paths()

    try:
        _materialize(output, tmp_dir)
        return _dispatch_build(tmp_dir, language, sql_dialect=sql_dialect)
    except Exception as exc:  # belt-and-suspenders — report infrastructure failure honestly
        return BuildResult(
            False, "build-runner-error",
            {_BUILD_KEY: [f"Build validation could not complete: {exc}"]},
            raw_output=f"build_runner internal error: {exc}",
        )


_MANIFESTS_BY_TOOL = {
    "cargo": "Cargo.toml", "gradle": "build.gradle.kts", "swift": "Package.swift",
    "sbt": "build.sbt", "clojure": "deps.edn", "flutter": _PUBSPEC_YAML,
    "Rscript": "DESCRIPTION", "julia": "Project.toml", "stack": "stack.yaml",
    "cabal": "*.cabal", "sbcl": "*.asd", "composer": "composer.json",
    "bash": "tests/smoke.sh",
    "bundle": "Gemfile",
    "dart": _PUBSPEC_YAML, "mix": "mix.exs",
}


# Function: _run_manifest_build
def _run_manifest_build(
    tmp_dir: Path, tool: str, args: list[str], after: list[str] | None = None,
) -> BuildResult:
    """Run a framework/package-manager build from the manifest-owning directory."""
    executable = _which(tool)
    if not executable:
        return BuildResult(False, _MISSING_TOOLCHAIN, {_BUILD_KEY: [f"{tool} not found on PATH"]})
    manifest = _find_one(tmp_dir, _MANIFESTS_BY_TOOL[tool])
    if not manifest:
        expected = _MANIFESTS_BY_TOOL[tool]
        return BuildResult(False, _MISSING_MANIFEST, {_BUILD_KEY: [f"Generated output has no {expected}"]})
    outputs: list[str] = []
    for command_args in (args, after):
        if not command_args:
            continue
        try:
            build_timeout = (
                _NPM_BUILD_TIMEOUT
                if tool in {"flutter", "stack", "swift"}
                else _BUILD_TIMEOUT
            )
            environment = (
                _java_build_environment(manifest)
                if tool == "gradle" else executable_environment(executable)
            )
            effective_args = list(command_args)
            if tool == "swift" and environment.get("SDKROOT"):
                effective_args.extend(["--sdk", environment["SDKROOT"]])
            proc = _run_process_tree(
                [executable, *effective_args], cwd=str(manifest.parent),
                timeout=build_timeout,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return BuildResult(False, f"{tool}-build", {_BUILD_KEY: [str(exc)]}, "\n".join(outputs))
        combined = (proc.stdout + "\n" + proc.stderr).strip()
        outputs.append(combined)
        if proc.returncode:
            # Verbose native builds can print the actionable compiler error
            # thousands of lines before a generic final "Build failed". Keep a
            # bounded selection of diagnostics as well as the tail so callers
            # can repair the real cause instead of retrying blind.
            diagnostic_lines = [
                line for line in combined.splitlines()
                if re.search(
                    r"(?i)(?:\berror(?:\[[^]]+\])?:|fatal(?:error)?:|"
                    r"undefined reference|cannot find|no rule to make target)",
                    line,
                )
            ]
            diagnostic = "\n".join(diagnostic_lines[-40:])
            tail = combined[-3000:]
            failure_detail = "\n".join(
                part for part in (diagnostic, tail) if part
            ) or f"{tool} build failed"
            return BuildResult(
                False, f"{tool}-build",
                {_BUILD_KEY: [failure_detail[-12000:]]},
                "\n".join(outputs),
            )
    return BuildResult(True, f"{tool}-build", raw_output="\n".join(outputs))


# Function: _run_cobol_project_build
def _run_cobol_project_build(tmp_dir: Path) -> BuildResult:
    """Compile and link every generated COBOL program independently."""
    executable = _which("cobc")
    if not executable:
        return BuildResult(False, _MISSING_TOOLCHAIN, {_BUILD_KEY: ["cobc not found on PATH"]})
    sources = sorted({*tmp_dir.rglob("*.cob"), *tmp_dir.rglob("*.cbl")})
    if not sources:
        return BuildResult(False, "missing-source", {_BUILD_KEY: ["Generated output contains no COBOL source files"]})
    outputs = []
    for index, source in enumerate(sources):
        output = tmp_dir / f"cobol-program-{index}.exe"
        try:
            proc = subprocess.run(
                [executable, "-x", "-std=ibm", str(source), "-o", str(output)],
                cwd=str(source.parent), capture_output=True, text=True, timeout=_BUILD_TIMEOUT,
                env=executable_environment(executable),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return BuildResult(False, "cobol-build", {_BUILD_KEY: [str(exc)]}, "\n".join(outputs))
        combined = (proc.stdout + "\n" + proc.stderr).strip()
        outputs.append(combined)
        if proc.returncode:
            return BuildResult(False, "cobol-build", {
                source.relative_to(tmp_dir).as_posix(): [combined[-3000:] or "COBOL build failed"],
            }, "\n".join(outputs))
    return BuildResult(True, "cobol-build", raw_output="\n".join(outputs))


# Function: _run_erlang_otp_build
def _run_erlang_otp_build(tmp_dir: Path) -> BuildResult:
    """Compile a complete OTP application and execute generated EUnit tests."""
    erlc, erl = _which("erlc"), _which("erl")
    if not (erlc and erl):
        return BuildResult(False, _MISSING_TOOLCHAIN, {_BUILD_KEY: ["erlc/erl not installed"]})
    sources = sorted(tmp_dir.rglob("*.erl"))
    if not sources:
        return BuildResult(False, "missing-source", {_BUILD_KEY: ["Generated output contains no Erlang source"]})
    ebin = tmp_dir / "__erlang_ebin"
    ebin.mkdir(exist_ok=True)
    environment = executable_environment(erlc)
    compile_proc = subprocess.run(
        [erlc, "-Werror", "-o", str(ebin), *map(str, sources)],
        cwd=str(tmp_dir), capture_output=True, text=True,
        timeout=_BUILD_TIMEOUT, env=environment,
    )
    compile_output = compile_proc.stdout + "\n" + compile_proc.stderr
    if compile_proc.returncode:
        return BuildResult(False, "erlang-build", {_BUILD_KEY: [compile_output[-3000:]]}, compile_output)
    tests = [source.stem for source in sources if source.stem.endswith("_tests")]
    if not tests:
        return BuildResult(True, "erlang-build", raw_output=compile_output)
    expression = f"case eunit:test([{','.join(tests)}], [verbose]) of ok -> halt(0); _ -> halt(1) end."
    test_proc = subprocess.run(
        [erl, "-noshell", "-pa", str(ebin), "-eval", expression],
        cwd=str(tmp_dir), capture_output=True, text=True,
        timeout=_BUILD_TIMEOUT, env=executable_environment(erl),
    )
    output = compile_output + test_proc.stdout + "\n" + test_proc.stderr
    return BuildResult(
        test_proc.returncode == 0, "erlang-build",
        {} if test_proc.returncode == 0 else {_BUILD_KEY: [output[-3000:]]},
        output,
    )


_SOURCE_CHECK_SPECS = {
    "python": ("python", ["-m", "compileall", "-q", "."]),
    "go": ("go", ["test", "./..."]),
    "php": ("php", None), "ruby": ("ruby", None),
    "cobol": ("cobc", None), "c": (_CLANG, None),
    "cpp": (_CLANG_CPP, None), "javascript": ("node", None),
}
_SOURCE_EXTENSIONS = {
    "php": ("*.php",), "ruby": ("*.rb",),
    "cobol": ("*.cob", "*.cbl", "*.cpy"),
    "c": ("*.c",), "cpp": ("*.cpp", "*.cc", "*.cxx"),
    "javascript": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
}


# Function: _source_check_executable
def _source_check_executable(language: str, command: str) -> str | None:
    if language == "python":
        return sys.executable
    if language == "php":
        return _which(command)
    if language == "c":
        return shutil.which(_CLANG) or shutil.which("gcc")
    if language == "cpp":
        return shutil.which(_CLANG_CPP) or shutil.which("g++")
    return shutil.which(command)


# Function: _source_files
def _source_files(tmp_dir: Path, language: str) -> list[Path]:
    return sorted({
        path
        for pattern in _SOURCE_EXTENSIONS[language]
        for path in tmp_dir.rglob(pattern)
    })


# Function: _source_commands
def _source_commands(
    executable: str, language: str, fixed_args: list[str] | None, files: list[Path],
) -> list[list[str]]:
    if fixed_args is not None:
        return [[executable, *fixed_args]]
    paths = [str(path) for path in files]
    if language in {"c", "cpp"}:
        return [[executable, "-fsyntax-only", *paths]]
    flags = {"cobol": "-fsyntax-only", "php": "-l", "ruby": "-c"}
    flag = flags.get(language, "--check")
    return [[executable, flag, path] for path in paths]


# Function: _execute_source_commands
def _execute_source_commands(
    commands: list[list[str]], tmp_dir: Path, language: str,
) -> BuildResult:
    output_parts = []
    for command in commands:
        proc = subprocess.run(
            command, cwd=str(tmp_dir), capture_output=True,
            text=True, timeout=_BUILD_TIMEOUT,
        )
        combined = (proc.stdout + "\n" + proc.stderr).strip()
        output_parts.append(combined)
        if proc.returncode != 0:
            return BuildResult(
                False, f"{language}-build",
                {_BUILD_KEY: [combined[-3000:] or "Build failed"]},
                "\n".join(output_parts),
            )
    return BuildResult(True, f"{language}-build", raw_output="\n".join(output_parts))


# Function: _python_test_commands
def _python_test_commands(executable: str, tmp_dir: Path) -> list[list[str]] | None:
    ignored_parts = {
        ".modernization-venv", ".venv", "venv", "site-packages",
        "node_modules", "vendor", "dist", "build",
    }
    test_files = [
        path for path in tmp_dir.rglob("test*.py")
        if not ignored_parts.intersection(part.casefold() for part in path.parts)
    ]
    if not test_files:
        return None
    # The Python standards pack selects pytest. Detect pytest-style functions
    # and execute the real framework instead of sending them through unittest
    # discovery, which can report zero tests while returning no syntax error.
    test_contents = [path.read_text(encoding="utf-8", errors="ignore") for path in test_files]
    uses_unittest = any("unittest.TestCase" in content for content in test_contents)
    if not uses_unittest and any(re.search(r"(?m)^(?:async\s+)?def\s+test_", content) for content in test_contents):
        project_roots = sorted({
            Path(path.relative_to(tmp_dir).parts[0])
            if len(path.relative_to(tmp_dir).parts) > 1 else Path(".")
            for path in test_files
        })
        pytest_script = (
            "import os,sys,pytest;"
            "os.chdir(sys.argv[1]);sys.path.insert(0,os.getcwd());"
            "raise SystemExit(pytest.main(['-q']))"
        )
        return [
            [executable, "-c", pytest_script, str(project_root)]
            for project_root in project_roots
        ]
    test_roots = sorted({path.parent.relative_to(tmp_dir) for path in test_files})
    discovery_script = (
        "import os,sys,unittest;"
        "os.chdir(sys.argv[1]);sys.path.insert(0,os.getcwd());"
        "suite=unittest.defaultTestLoader.discover(sys.argv[2],pattern='test*.py');"
        "result=unittest.TextTestRunner(verbosity=1).run(suite);"
        "raise SystemExit(0 if result.wasSuccessful() and result.testsRun else 1)"
    )
    commands = []
    for test_root in test_roots:
        parts = test_root.parts
        project_root = parts[0] if len(parts) > 1 else "."
        start_directory = str(Path(*parts[1:])) if len(parts) > 1 else str(test_root)
        commands.append([
            executable, "-c", discovery_script, project_root, start_directory,
        ])
    return commands


def _prepare_python_environment(tmp_dir: Path) -> tuple[str, BuildResult | None]:
    """Restore generated Python dependencies into an isolated project venv."""
    requirements = _find_one(tmp_dir, "requirements.txt")
    pyproject = _find_one(tmp_dir, "pyproject.toml")
    if not requirements and not pyproject:
        return sys.executable, None
    environment = tmp_dir / ".modernization-venv"
    try:
        create = subprocess.run(
            [sys.executable, "-m", "venv", str(environment)], capture_output=True,
            text=True, timeout=_BUILD_TIMEOUT, cwd=str(tmp_dir),
        )
        if create.returncode != 0:
            detail = (create.stdout + "\n" + create.stderr).strip()
            return sys.executable, BuildResult(False, "python-restore", {_INSTALL_KEY: [detail[-2000:]]})
        executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install_args = [str(executable), "-m", "pip", "install", "--disable-pip-version-check"]
        install_args.extend(["-r", str(requirements)] if requirements else ["-e", str(pyproject.parent)])
        install = subprocess.run(
            install_args, capture_output=True, text=True, timeout=_BUILD_TIMEOUT, cwd=str(tmp_dir),
        )
        if install.returncode != 0:
            detail = (install.stdout + "\n" + install.stderr).strip()
            return str(executable), BuildResult(False, "python-restore", {_INSTALL_KEY: [detail[-3000:]]})
        return str(executable), None
    except (OSError, subprocess.SubprocessError) as exc:
        return sys.executable, BuildResult(False, "python-restore", {_INSTALL_KEY: [str(exc)]})


# Function: _run_source_checks
def _run_source_checks(tmp_dir: Path, language: str) -> BuildResult:
    """Run a native whole-output syntax/build check for non-MSBuild/Maven stacks."""
    command, fixed_args = _SOURCE_CHECK_SPECS[language]
    executable = _source_check_executable(language, command)
    if not executable:
        return BuildResult(
            False, _MISSING_TOOLCHAIN,
            {_BUILD_KEY: [f"Required {language} build tool is not installed"]},
        )
    if language == "python":
        executable, restore_failure = _prepare_python_environment(tmp_dir)
        if restore_failure:
            return restore_failure
    files = [] if fixed_args is not None else _source_files(tmp_dir, language)
    if fixed_args is None and not files:
        return BuildResult(
            False, "missing-source",
            {_BUILD_KEY: [f"Generated output contains no {language} source files"]},
        )
    commands = _source_commands(executable, language, fixed_args, files)
    if language == "python":
        test_commands = _python_test_commands(executable, tmp_dir)
        if test_commands is None:
            return BuildResult(
                False, "missing-tests",
                {_BUILD_KEY: ["Python project validation requires generated unittest test files"]},
            )
        commands.extend(test_commands)
    execution_root = tmp_dir
    if language == "go":
        modules = sorted(tmp_dir.rglob("go.mod"))
        if len(modules) == 1:
            execution_root = modules[0].parent
    return _execute_source_commands(commands, execution_root, language)


# Function: _combine_build_results
def _combine_build_results(primary: BuildResult, frontend: BuildResult | None) -> BuildResult:
    """Fail a full-stack build when either backend or frontend fails."""
    if frontend is None:
        return primary
    errors = {**primary.errors_by_file}
    for path, messages in frontend.errors_by_file.items():
        errors.setdefault(path, []).extend(messages)
    for path, messages in list(errors.items()):
        errors[path] = list(dict.fromkeys(messages))
    effective = [result for result in (primary, frontend) if result.checker != "skipped"]
    if not effective:
        return BuildResult(True, "skipped", raw_output=primary.raw_output + "\n" + frontend.raw_output)
    return BuildResult(all(result.passed for result in effective), "+".join(result.checker for result in effective),
                       errors, primary.raw_output + "\n" + frontend.raw_output)
