# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: services/validators.py
# Date: 2026-07-18
# ---------------------------------------------------------------------------
"""
services/validators.py
Syntax-level validation for LLM-generated source files, used by modernizer.py
to gate generated code before it's returned as final output.

Real compiler checks are used wherever a compiler is actually available:
Python via py_compile, Java via the installed JDK's javac (dependency-
resolution diagnostics filtered out — no Maven classpath), TypeScript/JS via
a locally-vendored tsc, SQL via sqlglot, and C# via the .NET 8 SDK's own
Roslyn compiler (services/validators.py's _validate_csharp — invoked
directly as `csc.dll` against the SDK's reference assemblies, so it gets
real syntax AND semantic checking, e.g. "member does not exist on this
type", not just brace-balance). The structural heuristic (balanced braces,
no leftover markdown fences, no placeholder/TODO text) exists only as a
last-resort fallback for file types with no real checker at all (JSON/YAML/
Dockerfile/etc.) or for a deployment missing a required toolchain — it must
never be the checker used for a language that has a real compiler installed.
"""
from __future__ import annotations

import os
import ast
import builtins
import re
import json
import hashlib
import logging
import shutil
import subprocess
import tempfile
import tomllib
import threading
import yaml
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from services.native_toolchain import native_include_args
from services.tool_discovery import executable_environment, find_executable

logger = logging.getLogger(__name__)


class _TaggedYamlLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves CloudFormation/Ansible tagged values."""


# Function: _construct_yaml_tag
def _construct_yaml_tag(loader, _tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


_TaggedYamlLoader.add_multi_constructor("!", _construct_yaml_tag)


# Function: _refresh_windows_path
def _refresh_windows_path() -> None:
    """Merge machine/user PATH into long-running Windows service processes."""
    if os.name != "nt":
        return
    try:
        import winreg
        locations = (
            (winreg.HKEY_LOCAL_MACHINE, "SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        )
        values = [os.environ.get("PATH", "")]
        for hive, key_name in locations:
            with winreg.OpenKey(hive, key_name) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                values.append(os.path.expandvars(str(value)))
        entries = []
        seen = set()
        for value in values:
            for entry in value.split(os.pathsep):
                normalized = entry.strip().rstrip("\\").casefold()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    entries.append(entry.strip())
        os.environ["PATH"] = os.pathsep.join(entries)
    except (OSError, ValueError):
        pass


_refresh_windows_path()


# Function: _resolve_validator_command
def _resolve_validator_command(executable: str) -> str | None:
    """Resolve validators through the shared, allow-listed tool discovery."""
    return find_executable(executable)

# ─── Toolchain resolution (once, at import time) ──────────────────────────────

# Function: _resolve_javac
def _resolve_javac() -> str | None:
    return find_executable("javac")


_JAVAC_PATH = _resolve_javac()

_TSC_JS = Path(__file__).resolve().parent.parent / "tools" / "ts-validate" / "node_modules" / "typescript" / "lib" / "tsc.js"
_NODE_PATH = shutil.which("node")
_TSC_AVAILABLE = _TSC_JS.exists() and _NODE_PATH is not None

# ─── C# / .NET SDK (Roslyn) toolchain resolution ───────────────────────────────
# The .NET 8 SDK bundles its own modern Roslyn csc.dll (NOT the legacy
# ".NET Framework" csc.exe at C:\Windows\Microsoft.NET\Framework64\...\csc.exe,
# which only understands C# 5 and would false-fail on records/top-level
# statements). Invoking the SDK's own csc.dll directly against its reference
# assemblies gives a real compile — genuine syntax AND semantic errors, not a
# heuristic — without needing a full project (.csproj) or package restore.
_DOTNET_PATH = shutil.which("dotnet")


# Function: _highest_version_dir
def _highest_version_dir(parent: Path) -> Path | None:
    """Pick the highest-version subdirectory of `parent` (SDK/ref-pack
    installs are named e.g. "8.0.423" / "8.0.29") — sorts by parsed version
    tuple, not lexicographically, so "8.0.9" doesn't outrank "8.0.29"."""
    if not parent.is_dir():
        return None

    # Function: _version_key
    def _version_key(p: Path) -> tuple:
        try:
            return tuple(int(part) for part in p.name.split("."))
        except ValueError:
            return (-1,)

    candidates = sorted((d for d in parent.iterdir() if d.is_dir()), key=_version_key)
    return candidates[-1] if candidates else None


# Function: _resolve_csharp_compiler
def _resolve_csharp_compiler() -> tuple[Path | None, list[Path]]:
    """Locate the SDK's csc.dll and the full set of reference assemblies
    (.NET base class library + ASP.NET Core shared framework) needed to
    compile a single generated file in isolation without a real project.
    Returns (csc_dll_path_or_None, [reference_dll_paths])."""
    if not _DOTNET_PATH:
        return None, []
    dotnet_root = Path(_DOTNET_PATH).resolve().parent

    sdk_parent = _highest_version_dir(dotnet_root / "sdk")
    csc_dll = (sdk_parent / "Roslyn" / "bincore" / "csc.dll") if sdk_parent else None
    if not (csc_dll and csc_dll.exists()):
        return None, []

    ref_dlls: list[Path] = []
    for pack_name in ("Microsoft.NETCore.App.Ref", "Microsoft.AspNetCore.App.Ref"):
        pack_version_dir = _highest_version_dir(dotnet_root / "packs" / pack_name)
        if not pack_version_dir:
            continue
        ref_root = pack_version_dir / "ref"
        target_dir = _highest_version_dir(ref_root) if ref_root.is_dir() else None
        if target_dir:
            ref_dlls.extend(sorted(target_dir.glob("*.dll")))
    return csc_dll, ref_dlls


_CSC_DLL, _CS_REF_DLLS = _resolve_csharp_compiler()
_CSHARP_COMPILER_AVAILABLE = bool(_DOTNET_PATH and _CSC_DLL and _CS_REF_DLLS)

# Cache directory for the two static compile inputs that never change across
# calls: the reference-assembly response file (a raw arg list of 300+ "-r:"
# lines blows the Windows command-line length limit if passed inline, so csc
# needs it as an @response-file) and a synthetic global-usings file matching
# what the ASP.NET Core Web SDK's MSBuild targets auto-generate for a real
# project (<ImplicitUsings>enable</ImplicitUsings>) — without it, a perfectly
# correct minimal-API Program.cs fails to resolve WebApplication et al. purely
# because raw csc, unlike a real `dotnet build`, never sees that SDK-generated
# file.
_CS_TOOL_CACHE_DIR = Path(tempfile.gettempdir()) / "modernization_csharp_tools"
_CS_REFS_RSP = _CS_TOOL_CACHE_DIR / "refs.rsp"
_CS_IMPLICIT_USINGS_FILE = _CS_TOOL_CACHE_DIR / "ImplicitUsings.cs"

_CS_IMPLICIT_USINGS_CONTENT = (
    "global using global::System;\n"
    "global using global::System.Collections.Generic;\n"
    "global using global::System.IO;\n"
    "global using global::System.Linq;\n"
    "global using global::System.Net.Http;\n"
    "global using global::System.Net.Http.Json;\n"
    "global using global::System.Threading;\n"
    "global using global::System.Threading.Tasks;\n"
    "global using global::Microsoft.AspNetCore.Builder;\n"
    "global using global::Microsoft.AspNetCore.Hosting;\n"
    "global using global::Microsoft.AspNetCore.Http;\n"
    "global using global::Microsoft.AspNetCore.Routing;\n"
    "global using global::Microsoft.Extensions.Configuration;\n"
    "global using global::Microsoft.Extensions.DependencyInjection;\n"
    "global using global::Microsoft.Extensions.Hosting;\n"
    "global using global::Microsoft.Extensions.Logging;\n"
)

if _CSHARP_COMPILER_AVAILABLE:
    try:
        _CS_TOOL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CS_REFS_RSP.write_text(
            "\n".join(f'-r:"{dll}"' for dll in _CS_REF_DLLS), encoding="utf-8",
        )
        _CS_IMPLICIT_USINGS_FILE.write_text(_CS_IMPLICIT_USINGS_CONTENT, encoding="utf-8")
    except OSError:
        _CSHARP_COMPILER_AVAILABLE = False

try:
    import sqlglot
    from sqlglot.errors import ParseError as _SqlParseError, TokenError as _SqlTokenError
    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False

logging.getLogger("sqlfluff").setLevel(logging.WARNING)
try:
    from sqlfluff.core import Linter as _SqlFluffLinter
    _SQLFLUFF_AVAILABLE = True
except ImportError:
    _SQLFLUFF_AVAILABLE = False

# Free-text db_tech / target-stack descriptions -> sqlglot dialect codes.
# DB2/UDB has no native sqlglot dialect; "" (generic/ANSI) is the closest
# available real parse rather than skipping validation entirely for it.
_SQL_DIALECT_ALIASES = {
    "plpgsql": "postgres", "postgresql": "postgres", "postgres": "postgres",
    "mssql": "tsql", "sql server": "tsql", "sqlserver": "tsql", "tsql": "tsql", "t-sql": "tsql",
    "pl/sql": "oracle", "oracle": "oracle",
    "mysql": "mysql", "mariadb": "mysql",
    "db2": "db2", "udb": "db2",
    "sqlite": "sqlite", "bigquery": "bigquery", "snowflake": "snowflake",
    "redshift": "redshift", "duckdb": "duckdb", "databricks": "databricks",
    "spark": "spark", "hive": "hive", "trino": "trino", "presto": "presto",
    "clickhouse": "clickhouse", "teradata": "teradata",
}
_SQLFLUFF_DIALECTS = {
    "": "ansi", "bigquery": "bigquery", "clickhouse": "clickhouse",
    "databricks": "databricks", "db2": "db2", "duckdb": "duckdb",
    "hive": "hive", "mysql": "mysql", "oracle": "oracle",
    "postgres": "postgres", "redshift": "redshift", "snowflake": "snowflake",
    "spark": "sparksql", "sqlite": "sqlite", "teradata": "teradata",
    "trino": "trino", "tsql": "tsql",
}


# ─── Result type ───────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    path: str
    language: str
    checker: str            # "compiler" | "heuristic" | "skipped"
    passed: bool
    diagnostics: list[str] = field(default_factory=list)


# ─── Dispatcher ────────────────────────────────────────────────────────────────

_EXT_LANGUAGE = {
    ".py": "python",
    ".java": "java",
    ".ts": "typescript", ".tsx": "typescript",
    ".js": "typescript", ".jsx": "typescript",
    ".mjs": "typescript", ".cjs": "typescript",
    ".mts": "typescript", ".cts": "typescript",
    ".vue": "vue",
    ".cs": "csharp",
    ".sql": "sql",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cob": "cobol", ".cbl": "cobol", ".cpy": "cobol",
    ".php": "php", ".rb": "ruby", ".go": "go",
    ".rs": "rust", ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin",
    ".sh": "shell", ".bash": "shell", ".r": "r", ".scala": "scala",
    ".clj": "clojure", ".cljs": "clojure", ".cljc": "clojure",
    ".hs": "haskell", ".lhs": "haskell", ".lisp": "lisp", ".lsp": "lisp",
    ".cl": "lisp", ".el": "lisp", ".ex": "elixir", ".exs": "elixir",
    ".dart": "dart", ".jl": "julia", ".tf": "hcl", ".hcl": "hcl",
    ".proto": "protobuf", ".f": "fortran", ".for": "fortran",
    ".f90": "fortran", ".f95": "fortran", ".adb": "ada", ".ads": "ada",
    ".pas": "pascal", ".pp": "pascal", ".erl": "erlang", ".hrl": "erlang",
    ".ml": "ocaml", ".mli": "ocaml", ".pl": "prolog", ".pro": "prolog",
    ".abap": "abap", ".pli": "pli", ".pl1": "pli", ".rpg": "rpg",
    ".rpgle": "rpg", ".jcl": "jcl", ".m": "mumps", ".nsp": "natural",
    ".p": "progress4gl", ".cls": "apex", ".trigger": "apex",
    ".cshtml": "razor-template", ".vbhtml": "razor-template",
    ".jsp": "jsp-template", ".aspx": "aspnet-template", ".ascx": "aspnet-template",
    ".prisma": "prisma", ".cabal": "cabal", ".sbt": "sbt-build",
    ".gradle": "gradle-build",
    ".dockerfile": "dockerfile", ".jenkinsfile": "jenkinsfile",
}

# Infra/config/doc files that must never be validated against the target
# stack's source language, even though the per-file generation loop passes
# that language as a blanket hint for every file in the manifest (source AND
# infra alike). Without this, a file like "Dockerfile" or "docker-compose.yml"
# in a TypeScript-stack project falls through to language_hint="typescript",
# gets fed to tsc, fails as garbage TS, and burns retries "fixing" a Dockerfile
# into broken TypeScript-flavored nonsense — a real failure mode hit in testing.
_GENERIC_FILENAMES = {
    "dockerfile", "makefile", "readme.md", "license", ".gitignore", ".dockerignore",
    ".editorconfig", ".env", ".env.example",
}
_SPECIAL_FILENAME_LANGUAGE = {
    "jenkinsfile": "jenkinsfile",
    "cmakelists.txt": "cmake",
    "gemfile": "ruby", "rakefile": "ruby", "artisan": "php",
    "go.mod": "go-module", "go.sum": "dependency-lock",
    "description": "r-description",
    "schema.prisma": "prisma",
    "rebar.config": "erlang-config",
    "build.gradle.kts": "gradle-build", "settings.gradle.kts": "gradle-build",
}
_GENERIC_EXTS = {
    ".json", ".yaml", ".yml", ".md", ".txt", ".toml", ".xml", ".cfg", ".ini",
    ".properties", ".lock", ".csproj", ".fsproj", ".vbproj", ".sln",
    ".gitignore", ".conf", ".htm", ".html",
    ".css", ".scss", ".sass", ".less", ".svg", ".graphql", ".gql", ".razor",
    # IaC formats are stack-neutral artifacts.  They must be routed by their
    # own extension before the enclosing application's language hint; without
    # this a Bicep/Terraform file in a .NET project was sent to Roslyn and a
    # Terraform file in a Java project was sent to javac.
    ".bicep", ".bicepparam", ".tf", ".tfvars", ".hcl", ".graphqls",
    ".props", ".targets",
}

_LANGUAGE_ALIASES = {
    "py": "python", "python3": "python",
    "cs": "csharp", "c#": "csharp", "dotnet": "csharp", ".net": "csharp",
    "ts": "typescript", "tsx": "typescript", "javascript": "typescript", "js": "typescript", "jsx": "typescript",
    "postgresql": "sql", "postgres": "sql", "tsql": "sql", "plsql": "sql",
    "c++": "cpp", "cplusplus": "cpp", "golang": "go", "ruby-on-rails": "ruby",
    "cob": "cobol", "db2": "sql", "oracle": "sql", "mysql": "sql",
    "rs": "rust", "kt": "kotlin", "kotlin/jvm": "kotlin", "sh": "shell",
    "bash": "shell", "rscript": "r", "common lisp": "lisp",
    "terraform": "hcl", "terraform/hcl": "hcl", "fortran90": "fortran",
    "delphi": "pascal", "object pascal": "pascal", "pl/i": "pli",
    "progress": "progress4gl", "openedge abl": "progress4gl",
    "pl/sql": "sql", "t-sql": "sql",
}

_EXTERNAL_VALIDATORS = {
    "rust": (("rustc",), ["/source", "--edition", "2021", "--crate-type", "lib", "--emit", "metadata", "-o", "/tmp/output.rmeta"]),
    "swift": (("swiftc",), ["-parse", "/source"]),
    "kotlin": (("kotlinc",), ["/source", "-d", "/tmp/output.jar"]),
    "shell": (("bash",), ["-n", "/source"]),
    "r": (("Rscript",), ["-e", "parse(file=commandArgs(trailingOnly=TRUE)[1])", "/source"]),
    "scala": (("scalac",), ["-d", "/tmp/classes", "/source"]),
    "haskell": (("ghc",), ["-fno-code", "/source"]),
    "elixir": (("elixirc",), ["-o", "classes", "/source"]),
    "dart": (("dart",), ["analyze", "/source"]),
    "julia": (("julia",), [
        "--startup-file=no", "-e",
        "bad(x)=x isa Expr && (x.head in (:error,:incomplete) || any(bad,x.args));"
        "bad(Meta.parseall(read(ARGS[1],String))) && exit(1)",
        "/source",
    ]),
    "protobuf": (("protoc",), ["--descriptor_set_out=/tmp/output.pb", "--proto_path=/tmp", "/source"]),
    "fortran": (("flang-new", "gfortran"), ["-fsyntax-only", "/source"]),
    "ada": (("gnatmake",), ["-q", "-gnatc", "/source"]),
    "pascal": (("fpc",), ["-Cn", "-FE/tmp", "/source"]),
    "erlang": (("erlc",), ["-o", "/tmp/classes", "/source"]),
    "ocaml": (("ocamlc",), ["-c", "/source"]),
    "prolog": (("swipl",), ["-q", "-c", "/source"]),
}

# These 8 have no open-source compiler/interpreter that can run outside their
# proprietary vendor platform (SAP, IBM i/z-OS, Software AG, Progress, Salesforce)
# - Apex in particular cannot execute at all outside Salesforce's own org.
# Real compiler-backed validation is not achievable on this host; see
# _LEGACY_HEURISTIC_VALIDATORS below for the honest, structural-only alternative
# actually wired in for these languages (same tier as _validate_cobol's
# beyond-compilation structural checks, or _validate_dockerfile).
_UNAVAILABLE_VENDOR_TOOLCHAINS: dict[str, str] = {
    "abap": "an SAP ABAP system/compiler",
    "pli": "IBM Enterprise PL/I",
    "rpg": "an IBM i ILE RPG compiler",
    "jcl": "a z/OS JES environment",
    "mumps": "a configured M implementation",
    "natural": "Software AG Natural",
    "progress4gl": "Progress OpenEdge",
    "apex": "a Salesforce org compiler",
}

_TREE_SITTER_LANGUAGES = {
    # Every language handled by an optional external compiler also has a
    # deterministic parser route. A developer workstation may have rustc or
    # dart installed while a standalone deployment does not; absence of that
    # optional executable must not make an advertised single-file language
    # impossible to generate. When present, the native compiler remains the
    # preferred checker. Tree-sitter is used only when it is unavailable (or
    # when a compiler explicitly reports an infrastructure/autodetection
    # failure), and syntax errors still fail closed.
    "c": "c", "cpp": "cpp", "cobol": "cobol", "csharp": "csharp",
    "java": "java", "typescript": "typescript", "javascript": "javascript",
    "php": "php", "ruby": "ruby", "go": "go",
    "clojure": "clojure", "lisp": "commonlisp", "haskell": "haskell",
    "ocaml": "ocaml", "rust": "rust", "swift": "swift",
    "kotlin": "kotlin", "shell": "bash", "r": "r", "scala": "scala",
    "elixir": "elixir", "dart": "dart", "julia": "julia",
    "erlang": "erlang", "protobuf": "protobuf", "fortran": "fortran",
    "ada": "ada", "pascal": "pascal", "prolog": "prolog",
}


# Function: normalize_language
def normalize_language(language: str) -> str:
    value = (language or "").strip().lower()
    return _LANGUAGE_ALIASES.get(value, value)


# Function: detect_source_language
def detect_source_language(content: str, language_hint: str = "") -> str:
    """Identify a standalone generated source file without trusting form metadata alone.

    Strong, language-exclusive syntax receives more weight than shared constructs such
    as ``class``. The caller's stack hint only resolves genuinely ambiguous output.
    """
    text = (content or "").lstrip("\ufeff\r\n ")
    hint = normalize_language(language_hint)
    rules = {
        "python": (
            (r"^#!.*\bpython(?:3)?\b", 6), (r"^(?:from\s+[\w.]+\s+import\s+|import\s+[\w.]+)", 3),
            (r"(?m)^\s*(?:async\s+)?def\s+\w+\s*\([^\n]*\)\s*(?:->[^:]+)?\s*:", 5),
            (r"(?m)^\s*class\s+\w+(?:\([^\n]*\))?\s*:", 4), (r"\bNone\b|\bTrue\b|\bFalse\b", 1),
        ),
        "csharp": (
            (r"(?m)^\s*(?:global\s+)?using\s+[\w.]+\s*;", 4), (r"\bnamespace\s+[\w.]+\s*[;{]", 6),
            (r"\b(?:public|internal)\s+(?:sealed\s+|static\s+|abstract\s+|partial\s+)*(?:class|record|interface|struct)\s+\w+", 3),
            (r"\b(?:Task|IActionResult|IEnumerable|ILogger)<", 2),
        ),
        "java": (
            (r"(?m)^\s*package\s+[\w.]+\s*;", 6), (r"(?m)^\s*import\s+(?:static\s+)?[\w.*]+\s*;", 3),
            (r"\bpublic\s+(?:final\s+|abstract\s+)?(?:class|record|interface|enum)\s+\w+", 3),
            (r"\b(?:System\.out|Optional<|CompletableFuture<)", 2),
        ),
        "typescript": (
            (r"(?m)^\s*import\s+.+?\s+from\s+['\"]", 5), (r"(?m)^\s*export\s+(?:default\s+)?(?:class|interface|type|const|function)\b", 5),
            (r"\binterface\s+\w+(?:\s+extends\s+\w+)?\s*{", 4), (r"\b(?:const|let)\s+\w+\s*:\s*[A-Za-z_\{\[]", 3),
            (r"(?:<[A-Z][\w.]*\s*(?:/?>|\s+\w+=)|</[A-Z][\w.]*>)", 3),
        ),
        "sql": (
            (r"(?is)^\s*(?:--[^\n]*\n\s*)*(?:CREATE|ALTER)\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|PROCEDURE|FUNCTION|TRIGGER)\b", 6),
            (r"(?is)^\s*(?:WITH\s+\w+\s+AS\s*\(|SELECT\s+.+?\s+FROM\s+|INSERT\s+INTO\s+|UPDATE\s+\w+\s+SET\s+|DELETE\s+FROM\s+)", 5),
        ),
        "c": ((r"(?m)^\s*#include\s*<[\w./]+\.h>", 5), (r"\b(?:int|void)\s+main\s*\(", 4)),
        "cpp": ((r"(?m)^\s*#include\s*<(?:iostream|vector|string|memory|algorithm|map)>", 6), (r"\bstd::\w+", 5), (r"\bnamespace\s+\w+\s*{", 2)),
        "cobol": ((r"(?im)^\s*(?:IDENTIFICATION|ID)\s+DIVISION\s*\.", 8), (r"(?im)^\s*PROCEDURE\s+DIVISION", 6), (r"(?im)^\s*PROGRAM-ID\s*\.", 5)),
        "php": ((r"^<\?php\b", 8), (r"\$[A-Za-z_]\w*\s*=", 3), (r"\bnamespace\s+[\w\\]+\s*;", 3)),
        "ruby": ((r"(?m)^\s*require(?:_relative)?\s+['\"]", 4), (r"(?m)^\s*def\s+\w+[!?=]?\s*(?:\([^\n]*\))?\s*$", 5), (r"(?m)^\s*(?:class|module)\s+\w+(?:::\w+)*(?:\s*<\s*\w+)?\s*$", 4)),
        "go": ((r"(?m)^\s*package\s+\w+\s*$", 6), (r"(?m)^\s*import\s*(?:\(|\")", 4), (r"(?m)^\s*func\s+(?:\([^)]*\)\s*)?\w+\s*\(", 5)),
    }
    scores = {language: sum(weight for pattern, weight in patterns if re.search(pattern, text))
              for language, patterns in rules.items()}
    best_score = max(scores.values(), default=0)
    leaders = [language for language, score in scores.items() if score == best_score and score > 0]
    if best_score >= 4 and len(leaders) == 1:
        return leaders[0]
    if hint in leaders or (best_score < 4 and hint):
        return hint
    return leaders[0] if len(leaders) == 1 else (hint or "generic")


# Function: validate_file
def _validate_file_uncached(
    rel_path: str,
    content: str,
    language_hint: str = "",
    tmp_dir: Path | None = None,
    dialect_hint: str = "",
) -> ValidationResult:
    """Validate one generated file's syntax. Never raises — worst case returns
    a failing/skipped ValidationResult so callers can decide what to do.
    dialect_hint (SQL only): free text describing the target DB engine, e.g.
    a target stack's db_tech field ("PostgreSQL 16 + EF Core", "Oracle", ...)."""
    name = Path(rel_path).name.lower()
    ext = Path(rel_path).suffix.lower()
    normalized_path = str(rel_path).replace("\\", "/").casefold()
    if normalized_path == "conf/routes" or normalized_path.endswith("/conf/routes"):
        language = "play-routes"
    elif name.endswith(".app.src"):
        language = "erlang-config"
    elif name in _SPECIAL_FILENAME_LANGUAGE:
        language = _SPECIAL_FILENAME_LANGUAGE[name]
    elif ext in _EXT_LANGUAGE:
        language = _EXT_LANGUAGE[ext]
    elif name in _GENERIC_FILENAMES or name.startswith("dockerfile.") or ext in _GENERIC_EXTS or not ext:
        # No extension (Dockerfile, Makefile, ...) or a known infra/doc type —
        # never fall back to language_hint for these; see _GENERIC_FILENAMES.
        language = "generic"
    else:
        language = normalize_language(language_hint) or "generic"

    owns_tmp_dir = tmp_dir is None
    if owns_tmp_dir:
        tmp_dir = Path(tempfile.mkdtemp(prefix="modernization_validate_"))
    try:
        if language == "python":
            return _validate_python(rel_path, content, tmp_dir)
        if language == "java":
            return _validate_java(rel_path, content, tmp_dir)
        if language == "typescript":
            return _validate_typescript(rel_path, content, tmp_dir)
        if language == "csharp":
            return _validate_csharp(rel_path, content, tmp_dir)
        if language == "sql":
            return _validate_sql(rel_path, content, dialect_hint)
        if language == "vue":
            return _validate_vue_sfc(rel_path, content, tmp_dir)
        if language in {"c", "cpp"}:
            return _validate_c_family(rel_path, content, language, tmp_dir)
        if language == "cobol":
            return _validate_cobol(rel_path, content, tmp_dir, dialect_hint)
        if language == "php":
            return _validate_command(rel_path, content, language, tmp_dir, "php", ["-l"])
        if language == "ruby":
            return _validate_command(rel_path, content, language, tmp_dir, "ruby", ["-c"])
        if language == "go":
            return _validate_command(rel_path, content, language, tmp_dir, "gofmt", ["-e"])
        if language in _EXTERNAL_VALIDATORS:
            return _validate_external_language(rel_path, content, language, tmp_dir)
        if language in _TREE_SITTER_LANGUAGES:
            return _validate_tree_sitter(rel_path, content, language)
        if language in _UNAVAILABLE_VENDOR_TOOLCHAINS:
            prerequisite = _UNAVAILABLE_VENDOR_TOOLCHAINS[language]
            return ValidationResult(
                rel_path, language, "missing-toolchain", False,
                [f"Strict {language} validation requires {prerequisite}; it is not configured on this build host"],
            )
        if language in _LEGACY_HEURISTIC_VALIDATORS:
            return _LEGACY_HEURISTIC_VALIDATORS[language](rel_path, content)
        if language == "jenkinsfile":
            return _validate_jenkinsfile(rel_path, content)
        if language == "dockerfile":
            return _validate_dockerfile(rel_path, content)
        if language in {
            "cmake", "go-module", "dependency-lock", "r-description",
            "play-routes", "cabal", "erlang-config", "gradle-build",
            "sbt-build", "prisma", "razor-template", "jsp-template",
            "aspnet-template",
        }:
            return _validate_framework_artifact(rel_path, content, language)
        return _validate_generic(rel_path, content)
    finally:
        if owns_tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


try:
    _VALIDATION_CACHE_MAX = max(
        0, int(os.getenv("MODERNIZATION_VALIDATION_CACHE_SIZE", "2048")),
    )
except ValueError:
    _VALIDATION_CACHE_MAX = 2048
_VALIDATION_CACHE: OrderedDict[str, ValidationResult] = OrderedDict()
_VALIDATION_CACHE_LOCK = threading.RLock()
_VALIDATION_SINGLEFLIGHT: dict[str, threading.Lock] = {}
_VALIDATION_TOOLCHAIN_FINGERPRINT = "|".join((
    str(_JAVAC_PATH or ""), str(_DOTNET_PATH or ""), str(_TSC_JS), str(_NODE_PATH or ""),
))


def _validation_result_copy(result: ValidationResult) -> ValidationResult:
    return ValidationResult(
        result.path, result.language, result.checker, result.passed,
        list(result.diagnostics),
    )


def _clear_validation_cache() -> None:
    """Clear isolated-file validation memoization (primarily for tests/toolchain refresh)."""
    with _VALIDATION_CACHE_LOCK:
        _VALIDATION_CACHE.clear()
        _VALIDATION_SINGLEFLIGHT.clear()


def validate_file(
    rel_path: str,
    content: str,
    language_hint: str = "",
    tmp_dir: Path | None = None,
    dialect_hint: str = "",
) -> ValidationResult:
    """Validate a file, reusing only byte-identical isolated validation results.

    Project/build callers that provide ``tmp_dir`` always execute uncached because
    their result can depend on surrounding files. Generated single-file checks are
    content-addressed, so repaired content automatically misses the cache while
    unchanged files avoid repeated compiler subprocesses across generation phases.
    """
    if tmp_dir is not None or _VALIDATION_CACHE_MAX == 0:
        return _validate_file_uncached(
            rel_path, content, language_hint, tmp_dir, dialect_hint,
        )
    material = "\0".join((
        _VALIDATION_TOOLCHAIN_FINGERPRINT,
        str(rel_path).replace("\\", "/"),
        normalize_language(language_hint),
        str(dialect_hint or "").strip().casefold(),
        content,
    ))
    key = hashlib.sha256(material.encode("utf-8", errors="surrogatepass")).hexdigest()
    with _VALIDATION_CACHE_LOCK:
        cached = _VALIDATION_CACHE.get(key)
        if cached is not None:
            _VALIDATION_CACHE.move_to_end(key)
            return _validation_result_copy(cached)
        flight = _VALIDATION_SINGLEFLIGHT.setdefault(key, threading.Lock())
    with flight:
        with _VALIDATION_CACHE_LOCK:
            cached = _VALIDATION_CACHE.get(key)
            if cached is not None:
                _VALIDATION_CACHE.move_to_end(key)
                return _validation_result_copy(cached)
        result = _validate_file_uncached(
            rel_path, content, language_hint, None, dialect_hint,
        )
        with _VALIDATION_CACHE_LOCK:
            _VALIDATION_CACHE[key] = _validation_result_copy(result)
            _VALIDATION_CACHE.move_to_end(key)
            while len(_VALIDATION_CACHE) > _VALIDATION_CACHE_MAX:
                _VALIDATION_CACHE.popitem(last=False)
            _VALIDATION_SINGLEFLIGHT.pop(key, None)
        return result


# ─── Python ─────────────────────────────────────────────────────────────────────

# Function: _validate_python
def _python_definition_time_diagnostics(content: str) -> list[str]:
    """Find names that would fail while Python creates a class/function.

    Compilation alone accepts defaults such as ``Depends(get_session)`` even
    when those names are imported later or never defined. Python evaluates
    bases, decorators and defaults immediately during module import, so these
    are deterministic load-time failures that can be rejected without running
    untrusted generated code.
    """
    tree = ast.parse(content)
    available = set(dir(builtins)) | {
        # These module globals exist before Python executes user source.
        "__file__", "__name__", "__package__", "__spec__", "__loader__",
        "__cached__", "__builtins__",
    }
    diagnostics: list[str] = []
    declarations_started = False
    postponed_annotations = any(
        isinstance(statement, ast.ImportFrom)
        and statement.module == "__future__"
        and any(alias.name == "annotations" for alias in statement.names)
        for statement in tree.body
    )

    def loaded_names(node: ast.AST | None) -> set[tuple[str, int]]:
        if node is None:
            return set()
        return {
            (item.id, item.lineno)
            for item in ast.walk(node)
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
        }

    def check(expressions: list[ast.AST | None], context: str) -> None:
        for expression in expressions:
            for name, line in sorted(loaded_names(expression), key=lambda item: item[1]):
                if name not in available:
                    diagnostics.append(
                        f"line {line}: name {name!r} is used in {context} before it is defined or imported"
                    )

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            if declarations_started:
                diagnostics.append(
                    f"line {statement.lineno}: top-level import appears after declarations; "
                    "move all imports to the top of the file"
                )
            available.update(alias.asname or alias.name.split(".", 1)[0] for alias in statement.names)
        elif isinstance(statement, ast.ImportFrom):
            if declarations_started:
                diagnostics.append(
                    f"line {statement.lineno}: top-level import appears after declarations; "
                    "move all imports to the top of the file"
                )
            available.update(alias.asname or alias.name for alias in statement.names if alias.name != "*")
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            annotation_expressions = [] if postponed_annotations else [
                *(argument.annotation for argument in (
                    *statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs,
                ) if argument.annotation is not None),
                statement.returns,
            ]
            check([
                *statement.decorator_list, *statement.args.defaults,
                *(value for value in statement.args.kw_defaults if value is not None),
                *annotation_expressions,
            ], f"definition of {statement.name}")
            available.add(statement.name)
            declarations_started = True
        elif isinstance(statement, ast.ClassDef):
            check([*statement.decorator_list, *statement.bases, *(item.value for item in statement.keywords)],
                  f"definition of {statement.name}")
            available.add(statement.name)
            declarations_started = True
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = statement.value
            check([value], "module assignment")
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                available.update(
                    item.id for item in ast.walk(target)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                )
    return list(dict.fromkeys(diagnostics))


def _validate_python(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    try:
        compile(content, rel_path, "exec")
        diagnostics = _python_definition_time_diagnostics(content)
        return ValidationResult(
            rel_path, "python", "compiler", not diagnostics, diagnostics[:50],
        )
    except SyntaxError as exc:
        msg = f"line {exc.lineno}: {exc.msg}"
        return ValidationResult(rel_path, "python", "compiler", False, [msg])
    except (ValueError, TypeError) as exc:
        # e.g. null bytes in source — still a real defect in the generated content
        return ValidationResult(rel_path, "python", "compiler", False, [str(exc)])


# Function: _validate_external_language
def _validate_external_language(
    rel_path: str, content: str, language: str, tmp_dir: Path,
) -> ValidationResult:
    # ``mix.exs`` is executable project configuration, not a standalone
    # module that elixirc can compile outside a started Mix application.
    # Full-project validation runs Mix with dependencies; the single-file
    # gate must use the strict Elixir grammar parser here.
    if language == "elixir" and Path(rel_path).name.casefold() == "mix.exs":
        return _validate_tree_sitter(rel_path, content, language)
    commands, argument_template = _EXTERNAL_VALIDATORS[language]
    executable = next((find_executable(command) for command in commands if find_executable(command)), None)
    if not executable:
        if language in _TREE_SITTER_LANGUAGES:
            return _validate_tree_sitter(rel_path, content, language)
        return ValidationResult(
            rel_path, language, "missing-toolchain", False,
            [f"Strict {language} validation requires one of: {', '.join(commands)}"],
        )
    source_name = Path(rel_path).name
    if language == "erlang":
        module = re.search(r"(?m)^\s*-module\s*\(\s*([a-z][\w@]*)\s*\)\s*\.", content)
        if module:
            # erlc requires the physical filename to match -module(...). The
            # public single-file artifact is intentionally named generated.erl,
            # so compile a validation-only copy under its declared module name.
            source_name = f"{module.group(1)}.erl"
    source = tmp_dir / source_name
    source.write_text(content, encoding="utf-8")
    (tmp_dir / "classes").mkdir(exist_ok=True)
    arguments = [
        value.replace("/source", str(source)).replace("/tmp", str(tmp_dir))
        for value in argument_template
    ]
    try:
        proc = subprocess.run(
            [executable, *arguments], cwd=str(tmp_dir), capture_output=True,
            # encoding/errors: see _run_csc's comment — without this, a
            # compiler emitting a byte the host's default codepage can't
            # decode crashes subprocess.run()'s internal reader thread and
            # silently leaves stdout/stderr as None instead of raising here.
            text=True, timeout=60, env=executable_environment(executable),
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if language in _TREE_SITTER_LANGUAGES:
            logger.warning(
                "Native %s validator could not execute; using strict parser: %s",
                language, exc,
            )
            return _validate_tree_sitter(rel_path, content, language)
        return ValidationResult(rel_path, language, "compiler", False, [str(exc)])
    diagnostics = [
        line.strip() for line in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines()
        if line.strip()
    ]
    failed = proc.returncode != 0 or (
        language == "elixir"
        and any("Compilation error" in line or line.startswith("** (") for line in diagnostics)
    )
    infrastructure_failure = any(
        marker in line.casefold()
        for line in diagnostics
        for marker in (
            "compiler autodetection failed",
            "not supported by dkml",
            # Scala 2.13.8 can start and print its version on newer JDKs, then
            # crash in its own classfile reader before it reaches user source.
            # This is validator infrastructure failure, not a generated-code
            # diagnostic; the strict grammar parser remains authoritative.
            "bad constant pool index",
            "scala.reflect.internal.fatalerror",
            # Restricted Windows service identities can start swiftc but may
            # not execute its child swift-frontend binary. That is a host
            # boundary rather than a generated-source diagnostic.
            "permission denied",
        )
    )
    if failed and infrastructure_failure and language in _TREE_SITTER_LANGUAGES:
        return _validate_tree_sitter(rel_path, content, language)
    dependency_failure = any(
        marker in line.casefold()
        for line in diagnostics
        for marker in (
            "unresolved reference", "could not find module", "no such module",
            "not found: object", "not found: type", "package does not exist",
            "cannot find symbol", "unresolved module or unlinked crate",
            "unresolved import", "use of unresolved module",
            "is not loaded and could not be found",
            "target of uri doesn't exist",
        )
    )
    if failed and dependency_failure and language in _TREE_SITTER_LANGUAGES:
        # A standalone file has no Gradle/SwiftPM/Cabal dependency graph. Use
        # the strict grammar parser only for classpath/module-resolution-only
        # failures; genuine syntax errors remain parser failures.
        parsed = _validate_tree_sitter(rel_path, content, language)
        if parsed.passed:
            return parsed
        if language == "kotlin":
            # The bundled Kotlin grammar currently rejects some valid compact
            # annotation/test syntax. kotlinc is still authoritative: when
            # every compiler error is solely an absent Gradle dependency,
            # accept the syntax here and let the full Gradle build resolve the
            # classpath. Never suppress `expecting`/`syntax error` diagnostics.
            kotlin_errors = [
                line.casefold() for line in diagnostics if "error:" in line.casefold()
            ]
            if kotlin_errors and all("unresolved reference" in line for line in kotlin_errors):
                return ValidationResult(rel_path, language, "compiler", True, [])
    if failed and not diagnostics:
        diagnostics = [f"{Path(executable).name} exited with status {proc.returncode}"]
    return ValidationResult(
        rel_path, language, "compiler", not failed,
        [] if not failed else diagnostics[:50],
    )


# Function: _validate_tree_sitter
def _validate_tree_sitter(
    rel_path: str, content: str, language: str,
) -> ValidationResult:
    try:
        from tree_sitter_language_pack import get_parser
        tree = get_parser(_TREE_SITTER_LANGUAGES[language]).parse(content.encode("utf-8"))
    except (ImportError, LookupError, ValueError) as exc:
        return ValidationResult(
            rel_path, language, "missing-parser", False,
            [f"Strict {language} parser is unavailable: {exc}"],
        )
    if not tree.root_node.has_error:
        return ValidationResult(rel_path, language, "parser", True, [])
    diagnostics = []
    pending = [tree.root_node]
    while pending and len(diagnostics) < 20:
        node = pending.pop()
        if node.is_error or node.is_missing:
            row, column = node.start_point
            diagnostics.append(
                f"line {row + 1}, column {column + 1}: "
                f"{'missing ' + node.type if node.is_missing else 'syntax error'}"
            )
        pending.extend(reversed(node.children))
    return ValidationResult(
        rel_path, language, "parser", False,
        diagnostics or ["syntax tree contains errors"],
    )


# Function: _validate_command
def _validate_command(
    rel_path: str, content: str, language: str, tmp_dir: Path,
    executable: str, arguments: list[str],
) -> ValidationResult:
    """Run a language-native syntax checker without invoking a shell."""
    command = _resolve_validator_command(executable)
    if not command:
        if language in _TREE_SITTER_LANGUAGES:
            return _validate_tree_sitter(rel_path, content, language)
        return ValidationResult(
            rel_path, language, "skipped", False,
            [f"Required {executable} validator is not installed on the build host"],
        )
    suffix = Path(rel_path).suffix or {
        "cobol": ".cob", "php": ".php", "ruby": ".rb", "go": ".go",
    }.get(language, ".txt")
    source = tmp_dir / f"generated{suffix}"
    source.write_text(content, encoding="utf-8")
    command_env = os.environ.copy()
    if language == "cobol" and os.name == "nt":
        # MSYS2's native cobc.exe reports POSIX defaults such as
        # /ucrt64/share/gnucobol/config.  A Windows service is not running
        # inside an MSYS shell, so point it at the equivalent native paths.
        toolchain_root = Path(command).resolve().parent.parent
        config_dir = toolchain_root / "share" / "gnucobol" / "config"
        copy_dir = toolchain_root / "share" / "gnucobol" / "copy"
        if config_dir.is_dir():
            command_env["COB_CONFIG_DIR"] = str(config_dir)
        if copy_dir.is_dir():
            command_env["COB_COPY_DIR"] = str(copy_dir)
    try:
        proc = subprocess.run(
            [command, *arguments, str(source)], capture_output=True, text=True,
            timeout=30, cwd=str(tmp_dir), env=command_env,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if language in _TREE_SITTER_LANGUAGES:
            logger.warning(
                "Native %s validator could not execute; using strict parser: %s",
                language, exc,
            )
            return _validate_tree_sitter(rel_path, content, language)
        return ValidationResult(rel_path, language, "compiler", False, [str(exc)])
    diagnostics = [line.strip() for line in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if line.strip()]
    return ValidationResult(rel_path, language, "compiler", proc.returncode == 0,
                            [] if proc.returncode == 0 else diagnostics[:50])


# Function: _validate_cobol
def _validate_cobol(rel_path: str, content: str, tmp_dir: Path, dialect_hint: str) -> ValidationResult:
    """Compile COBOL with the requested dialect/format, then enforce structural invariants."""
    dialect = (dialect_hint or "").lower()
    ibm_fixed = any(token in dialect for token in ("ibm", "db2", "z/os", "zos", "enterprise cobol"))
    arguments = ["-fsyntax-only"]
    if ibm_fixed:
        arguments.extend(["-std=ibm", "-fformat=fixed", "-Wcolumn-overflow"])
    elif re.search(r"(?im)^\s*>>SOURCE\s+FORMAT\s+FREE", content):
        arguments.append("-fformat=free")
    result = _validate_command(rel_path, content, "cobol", tmp_dir, "cobc", arguments)
    if not result.passed:
        return result

    diagnostics: list[str] = []
    if ibm_fixed:
        for line_no, line in enumerate(content.splitlines(), 1):
            if "\t" in line:
                diagnostics.append(f"line {line_no}: fixed-format COBOL must not contain tabs")
            if len(line.rstrip()) > 72:
                diagnostics.append(f"line {line_no}: source extends beyond fixed-format column 72")
            if line.strip() and (len(line) < 7 or line[:6].strip() or line[6:7] not in {" ", "*", "-", "/", "D", "d"}):
                diagnostics.append(f"line {line_no}: invalid fixed-format columns 1-7")

    logical_lines = []
    for line in content.splitlines():
        if ibm_fixed:
            if len(line) > 6 and line[6] in {"*", "/"}:
                continue
            logical_lines.append(line[7:72] if len(line) > 7 else "")
        else:
            logical_lines.append(line)
    logical = "\n".join(logical_lines)

    program_match = re.search(r"(?i)\bPROGRAM-ID\s*\.\s*([A-Z0-9-]+)", logical)
    if program_match and len(program_match.group(1)) > 30:
        diagnostics.append("PROGRAM-ID must be 30 characters or fewer")

    select_names = {name.upper() for name in re.findall(r"(?i)\bSELECT\s+([A-Z0-9-]+)", logical)}
    fd_names = {name.upper() for name in re.findall(r"(?im)^\s*FD\s+([A-Z0-9-]+)", logical)}
    diagnostics.extend(f"SELECT {name} has no matching FD {name}" for name in sorted(select_names - fd_names))
    diagnostics.extend(f"FD {name} has no matching SELECT {name}" for name in sorted(fd_names - select_names))

    select_entries = re.findall(r"(?is)\bSELECT\s+([A-Z0-9-]+)(.*?)(?=\n\s*SELECT\b|\n\s*[A-Z-]+\s+DIVISION\b|\Z)", logical)
    for name, entry in select_entries:
        if not re.search(r"(?i)\bFILE\s+STATUS\s+IS\b", entry):
            diagnostics.append(f"SELECT {name.upper()} must declare FILE STATUS IS")

    paragraph_names = {
        name.upper() for name in re.findall(r"(?im)^\s*([0-9][A-Z0-9-]*)\s*\.\s*$", logical)
    }
    perform_targets = {
        name.upper() for name in re.findall(r"(?i)\bPERFORM\s+([0-9][A-Z0-9-]*)\b", logical)
    }
    diagnostics.extend(f"PERFORM target {name} is not defined" for name in sorted(perform_targets - paragraph_names))
    main_names = {name for name in paragraph_names if name.startswith("0000-")}
    exit_names = {name for name in paragraph_names if name.endswith("-EXIT")}
    diagnostics.extend(
        f"paragraph {name} is never PERFORMed"
        for name in sorted(paragraph_names - perform_targets - main_names - exit_names)
    )

    return ValidationResult(rel_path, "cobol", result.checker, not diagnostics, diagnostics[:50])


# Function: _validate_c_family
def _validate_c_family(rel_path: str, content: str, language: str, tmp_dir: Path) -> ValidationResult:
    # Prefer native LLVM on Windows. An MSYS2 gcc driver can be discoverable
    # on PATH while its cc1 child is not executable by a Windows service.
    compiler_names = ("clang", "gcc") if language == "c" else ("clang++", "g++")
    compiler = next((shutil.which(name) for name in compiler_names if shutil.which(name)), None)
    if not compiler:
        if language in _TREE_SITTER_LANGUAGES:
            return _validate_tree_sitter(rel_path, content, language)
        return ValidationResult(
            rel_path, language, "skipped", False,
            [f"Required {'C' if language == 'c' else 'C++'} compiler is not installed on the build host"],
        )
    suffix = ".c" if language == "c" else ".cpp"
    source = tmp_dir / f"generated{suffix}"
    source.write_text(content, encoding="utf-8")
    try:
        standard = "-std=c17" if language == "c" else "-std=c++23"
        proc = subprocess.run([compiler, standard, "-fsyntax-only", *native_include_args(), str(source)], capture_output=True,
                              text=True, timeout=30, cwd=str(tmp_dir),
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return ValidationResult(rel_path, language, "compiler", False, [str(exc)])
    diagnostics = [line.strip() for line in ((proc.stderr or "") + "\n" + (proc.stdout or "")).splitlines() if line.strip()]
    return ValidationResult(rel_path, language, "compiler", proc.returncode == 0,
                            [] if proc.returncode == 0 else diagnostics[:50])


# ─── Java ───────────────────────────────────────────────────────────────────────

_JAVA_PUBLIC_TYPE = re.compile(
    r"\bpublic\s+(?:final\s+|abstract\s+)?(?:class|interface|enum|record)\s+(\w+)"
)

# javac diagnostics that are only noise here because there's no Maven/Gradle
# classpath available offline, or that are an artifact of the generator's
# known two-type-per-file sentinel pattern (see modernizer.py _llm_domain_java/
# _llm_domain_csharp) — not real syntax defects.
_JAVAC_NOISE_PATTERNS = [
    re.compile(r"cannot find symbol"),
    re.compile(r"package [\w.]+ does not exist"),
    re.compile(r"cannot access"),
    re.compile(r"is public, should be declared in a file named"),
    # Standalone javac has no Maven test classpath.  Static imports from
    # AssertJ/Mockito/Spring therefore produce this secondary diagnostic even
    # though the owning types are supplied by spring-boot-starter-test during
    # the real reactor build.
    re.compile(r"static import only from classes and interfaces"),
]

# A one-file javac invocation has neither sibling project sources nor the
# Maven dependency classpath. Once a referenced type is unresolved, javac
# emits secondary semantic diagnostics that are not trustworthy in isolation.
# Keep only grammar/lexing failures here; Maven performs full type checking.
_JAVAC_SYNTAX_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"reached end of file while parsing",
        r"illegal (?:start of|character)",
        r"unclosed (?:string|character|comment)",
        r"(?:class|interface|enum|record) expected",
        r"(?:identifier|';'|'\)'|'\('|'\]'|'\}'|->) expected",
        r"not a statement",
        r"else without if",
        r"orphaned case",
        r"'?try'? without (?:'?catch'?|'?finally'?|catch, finally)",
        r"'?catch'? without '?try'?",
        r"'?finally'? without '?try'?",
    )
]

# These diagnostics are intrinsic to the generated compilation unit and stay
# trustworthy even when unrelated project/framework symbols are absent from a
# one-file javac classpath. In particular, blank-final constructor coverage is
# a local Java language rule and must not be hidden as dependency noise.
_JAVAC_INTRINSIC_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"variable\s+\w+\s+might not have been initialized",
        r"variable\s+\w+\s+might already have been assigned",
        r"missing return statement",
        r"unreachable statement",
    )
]

_JAVAC_ERROR_LINE = re.compile(r"^.*\.java:(\d+): error: (.*)$")


def _java_import_diagnostics(content: str) -> list[str]:
    """Recover common missing-JDK-import errors hidden by classpath-noise filtering."""
    diagnostics: list[str] = []
    common_types = {
        "List": "java.util.List",
        "Map": "java.util.Map",
        "Set": "java.util.Set",
        "Optional": "java.util.Optional",
        "UUID": "java.util.UUID",
        "Instant": "java.time.Instant",
        "LocalDate": "java.time.LocalDate",
        "LocalDateTime": "java.time.LocalDateTime",
        "BigDecimal": "java.math.BigDecimal",
    }
    imports = set(re.findall(r"(?m)^\s*import\s+([\w.*]+)\s*;", content))
    for type_name, qualified_name in common_types.items():
        if not re.search(rf"\b{type_name}\b", content):
            continue
        package_wildcard = qualified_name.rsplit(".", 1)[0] + ".*"
        if qualified_name not in imports and package_wildcard not in imports \
                and qualified_name not in content:
            diagnostics.append(f"missing required import {qualified_name}")
    return diagnostics


def _spring_boot3_semantic_diagnostics(content: str, rel_path: str = "") -> list[str]:
    """Reject Java that may parse but violates Spring Boot 3 application semantics."""
    if not re.search(r"org\.springframework|@(?:RestController|Controller|SpringBootApplication)\b", content):
        return []

    diagnostics: list[str] = []
    if Path(rel_path).name.endswith("Application.java"):
        misplaced = sorted(set(re.findall(
            r"import\s+((?:org\.springframework\.(?:web\.bind\.annotation|kafka\.annotation)|java\.util)\.[\w.*]+);",
            content,
        )))
        if misplaced:
            diagnostics.append(
                "Spring application bootstrap contains feature/controller imports that belong "
                "in dedicated components: " + ", ".join(misplaced)
            )
    legacy = sorted(set(re.findall(
        r"\bimport\s+(javax\.(?:servlet|persistence|validation|annotation|transaction|ws\.rs)[\w.*]*)\s*;",
        content,
    )))
    diagnostics.extend(
        f"Spring Boot 3 requires the Jakarta namespace; replace legacy import {package}"
        for package in legacy
    )

    is_test_source = "/src/test/" in rel_path.replace("\\", "/").casefold()
    # Match an actual annotated field declaration, not merely an injection
    # annotation followed by a constructor.  The former expression used a
    # negative method look-ahead that still classified constructors (which
    # have no return type) as fields, rejecting already-correct `private final`
    # constructor injection.  Requiring a Java declaration terminated by `;`
    # also covers package-private fields and common intervening annotations
    # such as @Qualifier without accepting constructor or setter injection.
    spring_field_injection = re.compile(
        r"(?m)^[ \t]*@(?:"
        r"(?:org\.springframework\.beans\.factory\.annotation\.)?Autowired|"
        r"(?:jakarta|javax)\.inject\.Inject|Inject|"
        r"(?:jakarta|javax)\.annotation\.Resource|Resource"
        r")(?:\s*\([^\r\n)]*\))?[ \t]*(?:\r?\n[ \t]*)?"
        r"(?:@[A-Za-z_$][\w.$]*(?:\s*\([^\r\n)]*\))?[ \t]*(?:\r?\n[ \t]*)?)*"
        r"(?:(?:private|protected|public)\s+)?"
        r"(?:(?:final|transient|volatile)\s+)*"
        r"[A-Za-z_$][\w.$]*(?:\s*<[^;{}\r\n]+>)?(?:\s*\[\])?\s+"
        r"[A-Za-z_$][\w$]*(?:\s*=\s*[^;\r\n]+)?\s*;[ \t]*$"
    )
    if not is_test_source and spring_field_injection.search(content):
        diagnostics.append(
            "Spring components must use constructor injection with final dependencies; field injection is forbidden"
        )

    if re.search(r"\b(?:RequestContextHolder|ServletRequestAttributes)\b", content):
        diagnostics.append(
            "Controllers must declare request data explicitly; RequestContextHolder/ServletRequestAttributes is forbidden"
        )

    is_controller = bool(
        re.search(r"@(?:RestController|Controller)\b", content)
        or "controller" in Path(rel_path).stem.casefold()
    )
    if is_controller:
        nested_transport_types = sorted(set(re.findall(
            r"\b(?:private|protected)\s+static\s+(?:final\s+)?"
            r"(?:class|record|enum)\s+(\w+)\b",
            content,
        )))
        if nested_transport_types:
            diagnostics.append(
                "Controller-owned nested transport types violate project contract ownership; "
                "move each DTO to its canonical manifest file: "
                + ", ".join(nested_transport_types)
            )

        boundary_dependencies = sorted({
            match.group(1)
            for match in re.finditer(
                r"\bprivate\s+final\s+([\w.]*?(?:Repository|EventPublisher|KafkaPublisher))\s+\w+\s*;",
                content,
            )
        })
        if boundary_dependencies:
            diagnostics.append(
                "Controllers may depend on an application service, not repositories or event "
                "publishers directly; move transaction/idempotency/event orchestration into "
                "the service layer: " + ", ".join(boundary_dependencies)
            )

        # Request DTOs normally live in separate files, so constraint presence
        # is a whole-project contract and cannot be judged from a controller
        # in isolation. Nested DTOs are rejected above and project validation
        # verifies the canonical DTO file.
        if (
            nested_transport_types
            and re.search(r"@Valid\s+@RequestBody\b", content)
            and not re.search(
                r"@(?:NotBlank|NotEmpty|NotNull|Positive|PositiveOrZero|Min|Max|Size|Pattern)\b",
                content,
            )
        ):
            diagnostics.append(
                "@Valid nested request body has no Jakarta Bean Validation constraints"
            )
    explicit_idempotency_header = re.search(
        r"@RequestHeader\s*\((?:(?!\)).)*(?:[\"']Idempotency-Key[\"']|IDEMPOTENCY[_A-Z]*)"
        r"(?:(?!\)).)*\)",
        content,
        re.DOTALL,
    )
    if is_controller and "Idempotency-Key" in content and not explicit_idempotency_header:
        diagnostics.append(
            "Idempotency-Key must be an explicit controller method parameter, for example "
            '@RequestHeader(name = "Idempotency-Key") String idempotencyKey; do not read it '
            "through HttpServletRequest, RequestContextHolder, or a local string constant"
        )

    if re.search(r"catch\s*\(\s*(?:Exception|Throwable)\b", content):
        diagnostics.append(
            "Broad Exception/Throwable catches are forbidden in Spring web code; use typed exceptions and centralized handling"
        )

    if re.search(r"\bWebSecurityConfigurerAdapter\b", content):
        diagnostics.append(
            "WebSecurityConfigurerAdapter was removed from the Spring Boot 3 security model; "
            "declare a SecurityFilterChain bean instead"
        )

    response_types = set(re.findall(r"\bResponseEntity\s*<\s*([\w.]+)\s*>", content))
    body_types = set(re.findall(r"\.body\s*\(\s*new\s+([\w.]+)\s*\(", content))
    if response_types and body_types:
        simple_responses = {name.rsplit(".", 1)[-1] for name in response_types}
        simple_bodies = {name.rsplit(".", 1)[-1] for name in body_types}
        mismatches = sorted(
            body for body in simple_bodies
            if body not in simple_responses and "Object" not in simple_responses
        )
        if mismatches:
            diagnostics.append(
                "ResponseEntity generic/body type mismatch involving: " + ", ".join(mismatches)
            )
    return diagnostics


def _jakarta_ws_rs_semantic_diagnostics(content: str) -> list[str]:
    """Reject known nonexistent JAX-RS factories hidden by classpath filtering."""
    if "jakarta.ws.rs.core.Response" not in content:
        return []
    diagnostics: list[str] = []
    if re.search(r"\bResponse\.locationBuilder\s*\(", content):
        diagnostics.append(
            "jakarta.ws.rs.core.Response has no locationBuilder(String) method; "
            "use jakarta.ws.rs.core.UriBuilder.fromPath(...).build()"
        )
    if re.search(r"\bResponse\.notFound\s*\(", content):
        diagnostics.append(
            "jakarta.ws.rs.core.Response has no notFound(...) factory; "
            "use Response.status(Response.Status.NOT_FOUND).build()"
        )
    return diagnostics


# Function: _validate_java
def _validate_java(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    semantic_diagnostics = (
        _java_import_diagnostics(content)
        + _spring_boot3_semantic_diagnostics(content, rel_path)
        + _jakarta_ws_rs_semantic_diagnostics(content)
    )
    source_name = Path(rel_path).name
    normalized_path = rel_path.replace("\\", "/")
    java_root_marker = "/src/main/java/"
    if java_root_marker in normalized_path:
        package_path = normalized_path.split(java_root_marker, 1)[1].rsplit("/", 1)[0]
        expected_package = package_path.replace("/", ".")
        declared_package = re.search(
            r"(?m)^\s*package\s+([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*;",
            content,
        )
        if not declared_package:
            semantic_diagnostics.append(
                f"Java source {source_name} must declare path-matching package {expected_package}"
            )
        elif declared_package.group(1) != expected_package:
            semantic_diagnostics.append(
                f"Java source {source_name} package {declared_package.group(1)} does not match "
                f"source path package {expected_package}"
            )
    if source_name not in {"package-info.java", "module-info.java"}:
        expected_type = Path(rel_path).stem
        declared_types = set(re.findall(
            r"\b(?:class|interface|enum|record|@interface)\s+([A-Za-z_]\w*)",
            content,
        ))
        if not declared_types:
            semantic_diagnostics.append(
                f"Java source {source_name} contains no type declaration; declare {expected_type}"
            )
        elif expected_type not in declared_types:
            semantic_diagnostics.append(
                f"Java source {source_name} must declare its filename-matching type {expected_type}"
            )
    if not _JAVAC_PATH:
        parsed = _validate_tree_sitter(rel_path, content, "java")
        if semantic_diagnostics:
            return ValidationResult(
                rel_path, "java", parsed.checker, False,
                [*semantic_diagnostics, *parsed.diagnostics],
            )
        return parsed

    match = _JAVA_PUBLIC_TYPE.search(content)
    type_name = match.group(1) if match else Path(rel_path).stem
    file_path = tmp_dir / f"{type_name}.java"
    file_path.write_text(content, encoding="utf-8")

    try:
        proc = subprocess.run(
            [_JAVAC_PATH, "-d", str(tmp_dir), "-nowarn", str(file_path)],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return ValidationResult(rel_path, "java", "compiler", False, [f"javac invocation failed: {exc}"])

    if proc.returncode == 0:
        return ValidationResult(
            rel_path, "java", "compiler", not semantic_diagnostics, semantic_diagnostics,
        )

    diagnostics: list[str] = list(semantic_diagnostics)
    compiler_errors = [
        (match.group(1), match.group(2))
        for line in (proc.stderr or "").splitlines()
        if (match := _JAVAC_ERROR_LINE.match(line))
    ]
    has_classpath_noise = any(
        pattern.search(message)
        for _lineno, message in compiler_errors
        for pattern in _JAVAC_NOISE_PATTERNS
    )
    for lineno, message in compiler_errors:
        if any(pattern.search(message) for pattern in _JAVAC_NOISE_PATTERNS):
            continue
        trustworthy_with_missing_classpath = any(
            pattern.search(message)
            for pattern in (*_JAVAC_SYNTAX_PATTERNS, *_JAVAC_INTRINSIC_PATTERNS)
        )
        if not has_classpath_noise or trustworthy_with_missing_classpath:
            diagnostics.append(f"line {lineno}: {message}")

    return ValidationResult(rel_path, "java", "compiler", len(diagnostics) == 0, diagnostics)


# ─── TypeScript / JavaScript ────────────────────────────────────────────────────

_TSC_DIAG = re.compile(r"(?:\((\d+),(\d+)\):\s*)?error TS(\d+):\s*(.*)$")
_TSC_SYNTAX_CODE_RANGES = ((1000, 2000), (17000, 18000))


# Function: _contains_jsx
def _contains_jsx(content: str) -> bool:
    """Conservatively identify JSX grammar without mistaking TS generics/casts."""
    text = content or ""
    # HTML-looking text inside JavaScript strings/template literals is data,
    # not JSX (Vue's createApp({template: "<main>..."}) exposed this gap).
    # Keep the original text only for the React-import signal below.
    syntax_text = _strip_strings_and_comments(text)
    # TypeScript generic arrow functions and angle-bracket assertions use the
    # same leading token shape as JSX.  In particular, generated API clients
    # commonly contain `const parseJson = <T>(...) => ...`; treating `<T>` as
    # an opening component tag incorrectly forces a valid .ts file to .tsx.
    # Mask only the type-parameter/assertion prefix when it is immediately
    # followed by a call/parameter list. Real JSX with a closing or self-
    # closing tag remains visible to the checks below.
    jsx_text = re.sub(
        r"(?P<prefix>\breturn\s+|=>\s*|=\s*)"
        r"<\s*[A-Za-z_$][\w$]*(?:\s+extends\s+[^>,]+)?"
        r"(?:\s*,\s*[A-Za-z_$][\w$]*(?:\s+extends\s+[^>,]+)?)*\s*>"
        r"(?=\s*\()",
        lambda match: match.group("prefix"),
        syntax_text,
    )
    return bool(
        re.search(r"</[A-Za-z][\w.:-]*\s*>", jsx_text)
        or re.search(r"<>\s*[\s\S]*?</>", jsx_text)
        # An opening tag without attributes is already covered by the closing
        # and self-closing checks. Requiring an attribute or `/>` here avoids
        # classifying valid TypeScript assertions such as `<string>value` as
        # JSX while still recognizing `<Widget name={value}>`.
        or re.search(
            r"(?:return|=>|=)\s*(?:\(\s*)?<[A-Za-z][\w.:-]*"
            r"(?:\s+[A-Za-z_:][\w:.-]*(?:\s*=\s*[^>]+)?\s*>|\s*/>)",
            jsx_text,
        )
        or (
            re.search(r"(?m)^\s*import\s+.+?\s+from\s+['\"]react(?:/[^'\"]*)?['\"]", text)
            and re.search(r"<[A-Za-z][\w.:-]*\b[^>]*?/\s*>", jsx_text)
        )
    )


# Function: _validate_typescript
def _validate_typescript(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    if not _TSC_AVAILABLE:
        parser_language = (
            "javascript"
            if Path(rel_path).suffix.casefold() in {".js", ".jsx"}
            else "typescript"
        )
        return _validate_tree_sitter(rel_path, content, parser_language)

    suffix = Path(rel_path).suffix or ".ts"
    if suffix.lower() == ".ts" and _contains_jsx(content):
        return ValidationResult(
            rel_path, "typescript", "compiler", False,
            ["TS17004: JSX syntax requires a .tsx filename; plain .ts files cannot contain JSX"],
        )
    file_path = tmp_dir / f"file{suffix}"
    file_path.write_text(content, encoding="utf-8")

    normalized_path = rel_path.replace("\\", "/").casefold()
    decorator_module_markers = (
        "'@angular/", '"@angular/', "'@nestjs/", '"@nestjs/',
        "'typeorm'", '"typeorm"', "'class-validator'", '"class-validator"',
        "'inversify'", '"inversify"',
        "'routing-controllers'", '"routing-controllers"',
    )
    decorator_framework = bool(
        ("/frontend/src/app/" in f"/{normalized_path}" and suffix.casefold() == ".ts")
        or any(marker in content for marker in decorator_module_markers)
    )
    compiler_args = [
        _NODE_PATH, str(_TSC_JS),
        "--noEmit", "--skipLibCheck", "--allowJs", "--jsx", "react",
        "--target", "es2020", "--module", "esnext",
    ]
    if decorator_framework:
        # Angular/Nest projects generated by this service explicitly enable
        # legacy decorators in tsconfig.  The isolated preflight must compile
        # under that same syntax mode or valid constructor parameter injection
        # fails with TS1206 before the real project build can run.
        compiler_args.append("--experimentalDecorators")
    compiler_args.append(str(file_path))

    try:
        proc = subprocess.run(
            compiler_args,
            capture_output=True, text=True, timeout=30, cwd=str(tmp_dir),
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return ValidationResult(rel_path, "typescript", "compiler", False, [f"tsc invocation failed: {exc}"])

    diagnostics: list[str] = []
    for line in (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines():
        m = _TSC_DIAG.search(line)
        if not m:
            continue
        line_number, column_number, code_text, message = m.groups()
        code = int(code_text)
        if any(start <= code < end for start, end in _TSC_SYNTAX_CODE_RANGES):
            # TS1xxx and TS17xxx = syntactic/JSX parse errors — always real.
            # TS2xxx+ = semantic/type/module-resolution errors — expected
            # noise since the generated project has no installed node_modules.
            location = (
                f"line {line_number}:{column_number}: "
                if line_number and column_number else ""
            )
            diagnostics.append(f"{location}TS{code}: {message}")

    return ValidationResult(rel_path, "typescript", "compiler", len(diagnostics) == 0, diagnostics)


# ─── C# ─────────────────────────────────────────────────────────────────────────

_CS_ERROR_RE = re.compile(r"^.+?\((\d+),\d+\):\s*error\s+(CS\d+):\s*(.*)$")

# Diagnostics that are only noise when compiling ONE file in isolation with no
# other project files present: a real reference to a sibling project type
# (IWidgetService, WidgetDto, ...) necessarily can't resolve here, since only
# this file plus the BCL/ASP.NET Core reference assemblies are on the compile
# — that's expected, not a defect (Phase 2's real whole-project `dotnet build`
# is what catches genuine cross-file problems). CS8805 is purely an artifact
# of this validator's own library-vs-exe entry-point detection (see below),
# never a real defect in the generated content.
_CS_NOISE_CODES = {"CS0246", "CS0234", "CS0103", "CS8805"}


# Function: _validate_csharp
def _validate_csharp(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    if not _CSHARP_COMPILER_AVAILABLE:
        # Tree-sitter is a strict grammar parser, not the former brace-counting
        # heuristic. Roslyn remains preferred whenever the SDK is installed.
        return _validate_tree_sitter(rel_path, content, "csharp")

    file_path = tmp_dir / (Path(rel_path).name or "File.cs")
    file_path.write_text(content, encoding="utf-8")
    usings_copy = tmp_dir / "__ImplicitUsings.cs"
    usings_copy.write_text(_CS_IMPLICIT_USINGS_CONTENT, encoding="utf-8")

    # A file with top-level statements (most often Program.cs) can only be
    # compiled as an executable; a normal class/interface/record file can
    # only be compiled as a library — each fails outright in the other mode
    # (CS8805 / CS5001 respectively). Try the common case (library) first;
    # retry as an executable only if that specific mismatch is what failed.
    proc = _run_csc(file_path, usings_copy, tmp_dir, "library")
    # Defense in depth alongside _run_csc's own encoding fix above: never let
    # a None stdout/stderr (whatever the cause) turn a compiler-diagnostics
    # check into an unhandled TypeError that kills the entire modernization
    # job after everything else already succeeded.
    if proc.returncode != 0 and "CS8805" in (proc.stderr or "") + (proc.stdout or ""):
        proc = _run_csc(file_path, usings_copy, tmp_dir, "exe")

    if proc.returncode == 0:
        return ValidationResult(rel_path, "csharp", "compiler", True, [])

    diagnostics: list[str] = []
    matched_any_error_line = False
    for line in ((proc.stdout or "") + "\n" + (proc.stderr or "")).splitlines():
        m = _CS_ERROR_RE.match(line.strip())
        if not m:
            continue
        matched_any_error_line = True
        lineno, code, message = m.groups()
        if code in _CS_NOISE_CODES:
            continue
        diagnostics.append(f"line {lineno} {code}: {message}")

    if not diagnostics and not matched_any_error_line:
        # A nonzero exit where we couldn't identify even one CSxxxx line to
        # filter is not the same thing as "every reported error was noise" —
        # most often it means the compiler's output went missing outright
        # (undecodable bytes, an empty buffer; see _run_csc's encoding fix)
        # rather than there being nothing wrong. Report it instead of
        # silently treating "nothing parsed" as "nothing to report".
        diagnostics = [f"csc exited with code {proc.returncode} but produced no parseable diagnostics"]

    return ValidationResult(rel_path, "csharp", "compiler", len(diagnostics) == 0, diagnostics)


# Function: _run_csc
def _run_csc(file_path: Path, usings_path: Path, tmp_dir: Path, target_kind: str) -> subprocess.CompletedProcess[str]:
    out_ext = "exe" if target_kind == "exe" else "dll"
    try:
        return subprocess.run(
            [
                _DOTNET_PATH, str(_CSC_DLL), "-nologo", f"-t:{target_kind}",
                f"-out:{tmp_dir / f'out.{out_ext}'}",
                f"@{_CS_REFS_RSP}", str(usings_path), str(file_path),
            ],
            # Without an explicit encoding, `text=True` decodes with
            # locale.getpreferredencoding() — cp1252 on this host — and csc's
            # output routinely contains UTF-8 bytes (em dashes, smart quotes,
            # non-ASCII identifiers an LLM emitted in a comment or string
            # literal). A byte cp1252 can't decode crashes the internal
            # communicate() reader thread with an uncaught UnicodeDecodeError
            # that subprocess.run() itself never sees, silently leaving
            # proc.stdout/proc.stderr as None instead of raising here — which
            # then blew up every caller that concatenates them assuming str.
            # errors="replace" guarantees a populated string either way.
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=f"csc invocation failed: {exc}")


# ─── SQL ────────────────────────────────────────────────────────────────────────

# Function: _resolve_sql_dialect
def _resolve_sql_dialect(dialect_hint: str) -> str:
    hint = (dialect_hint or "").lower()
    for needle, dialect in _SQL_DIALECT_ALIASES.items():
        if needle in hint:
            return dialect
    return ""  # generic/ANSI parse — still real, just dialect-agnostic


# Function: _infer_sql_dialect
def _infer_sql_dialect(content: str) -> str:
    """Infer a dialect only from syntax that is strongly vendor-specific.

    Every pattern here must be a signal that genuinely cannot appear in any
    other dialect's SQL — a false positive fires the "SQL dialect mismatch"
    hard-failure in _validate_sql against perfectly valid output. Plain
    ``VARCHAR(n)`` is standard ANSI SQL used by Postgres, MySQL, and T-SQL
    alike; only the ``N``-prefixed Unicode variant (``NVARCHAR``) is actually
    SQL-Server/Sybase-specific. Likewise bare ``IDENTITY(seed, increment)``
    right after a column type is T-SQL, but Postgres's own
    ``GENERATED ... AS IDENTITY (START WITH n INCREMENT BY n)`` also matches
    ``IDENTITY (`` — so only match when not immediately preceded by "AS ".
    """
    # Dialect keywords inside comments and string literals are documentation or
    # data, not executable syntax.  Inspecting the raw source made an Oracle
    # comment such as "raise exception when no row exists" look like Postgres
    # ``RAISE EXCEPTION`` and fail the authoritative Oracle target.
    executable = _mask_sql_literals_and_comments(
        _normalize_oracle_sqlplus_commands(content),
    )
    signals = (
        ("postgres", (
            r"\bLANGUAGE\s+plpgsql\b", r"\$\$",
            r"\bRAISE\s+(?:EXCEPTION|NOTICE)\b", r"::[A-Za-z_]\w*",
        )),
        ("tsql", (
            r"(?m)^\s*GO\s*$", r"\bCREATE\s+OR\s+ALTER\b",
            r"\bTRY_CONVERT\b", r"\bSCOPE_IDENTITY\s*\(",
            r"\bBEGIN\s+(?:TRY|CATCH)\b",
            r"\bsys\.tables\b", r"\bSYSUTCDATETIME\s*\(",
            r"\bNVARCHAR\s*\(", r"\bDATETIME2\s*\(",
            r"(?<!AS )(?<!as )\bIDENTITY\s*\(",
        )),
        ("oracle", (
            r"\bVARCHAR2\b", r"\bSYS_REFCURSOR\b",
            r"\bDBMS_[A-Za-z_]\w*\b", r"\bRAISE_APPLICATION_ERROR\s*\(",
        )),
        ("mysql", (
            r"(?m)^\s*DELIMITER\b", r"\bAUTO_INCREMENT\b",
            r"\bSIGNAL\s+SQLSTATE\b",
        )),
        ("db2", (
            r"\bSYSIBM\.SYSDUMMY1\b", r"\bBEGIN\s+ATOMIC\b",
            r"\bSPECIFIC\s+PROCEDURE\b",
        )),
    )
    for dialect, patterns in signals:
        if any(re.search(pattern, executable, re.IGNORECASE) for pattern in patterns):
            return dialect
    return ""


# Function: _sql_safety_diagnostics
def _sql_safety_diagnostics(content: str) -> list[str]:
    diagnostics = []
    for line_number, line in enumerate((content or "").splitlines(), 1):
        code = line.split("--", 1)[0]
        if not re.match(r"^\s*(?:WHERE|AND|OR)\b", code, re.IGNORECASE):
            continue
        comparison = re.search(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\b",
            code,
            re.IGNORECASE,
        )
        if comparison and comparison.group(1).casefold() == comparison.group(2).casefold():
            diagnostics.append(
                f"line {line_number}: tautological predicate "
                f"`{comparison.group(1)} = {comparison.group(2)}`; qualify the column "
                "and use a distinctly named parameter"
            )
    return diagnostics


def _mask_sql_literals_and_comments(content: str) -> str:
    """Preserve SQL layout while hiding literals/comments from structure scans."""
    source = content or ""
    masked = list(source)
    index = 0
    pairs = {"[": "]", "(": ")", "{": "}", "<": ">"}
    while index < len(source):
        if source.startswith("--", index):
            end = source.find("\n", index + 2)
            end = len(source) if end < 0 else end
            for position in range(index, end):
                masked[position] = " "
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
            for position in range(index, end):
                if source[position] not in "\r\n":
                    masked[position] = " "
            index = end
            continue
        if source[index:index + 2].casefold() == "q'" and index + 2 < len(source):
            opener = source[index + 2]
            closer = pairs.get(opener, opener)
            end = source.find(closer + "'", index + 3)
            end = len(source) if end < 0 else end + 2
            for position in range(index, end):
                if source[position] not in "\r\n":
                    masked[position] = " "
            index = end
            continue
        if source[index] == "'":
            end = index + 1
            while end < len(source):
                if source[end] == "'":
                    if end + 1 < len(source) and source[end + 1] == "'":
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            for position in range(index, end):
                if source[position] not in "\r\n":
                    masked[position] = " "
            index = end
            continue
        index += 1
    return "".join(masked)


def _normalize_oracle_sqlplus_commands(content: str) -> str:
    """Translate SQL*Plus client commands for parser compatibility only.

    SQL*Plus commands are not Oracle SQL statements. SQLFluff can consume part
    of a script before reporting a later ``REM``/``SET SERVEROUTPUT``/bind
    command as unparsable SQL. Keep line count and the persisted deliverable
    unchanged while presenting these client-owned lines as parser comments.
    Deliberately do not match PL/SQL ``EXECUTE IMMEDIATE``.
    """
    command = re.compile(
        r"(?im)^([ \t]*)(?:"
        r"REM(?:[ \t]+[^\r\n]*)?"
        r"|PROMPT(?:[ \t]+[^\r\n]*)?"
        r"|SET[ \t]+(?:SERVEROUTPUT|DEFINE|ECHO|FEEDBACK|HEADING|LINESIZE|PAGESIZE|"
        r"TERMOUT|TIMING|VERIFY|SQLPROMPT)\b[^\r\n]*"
        r"|SPOOL\b[^\r\n]*"
        r"|WHENEVER[ \t]+(?:SQLERROR|OSERROR)\b[^\r\n]*"
        r"|(?:VARIABLE|VAR|PRINT|DEFINE|UNDEFINE|ACCEPT|COLUMN|PAUSE)\b[^\r\n]*"
        r"|EXEC(?:UTE)?[ \t]+(?!IMMEDIATE\b)[^\r\n]*"
        r")[ \t]*$",
    )
    return command.sub(
        lambda match: f"{match.group(1)}-- SQLPLUS: {match.group(0).strip()}",
        content or "",
    )


def _oracle_plsql_diagnostics(content: str) -> list[str]:
    """Catch package errors that SQLFluff reports only as an opaque body error."""
    masked = _mask_sql_literals_and_comments(content)
    declaration = re.compile(
        r"(?im)^\s*CREATE\s+OR\s+REPLACE\s+PACKAGE\s+(?:(BODY)\s+)?"
        r"(\"?[A-Za-z][A-Za-z0-9_$#]*\"?)\b.*?\b(?:AS|IS)\b"
    )
    units = list(declaration.finditer(masked))
    diagnostics: list[str] = []
    # The layout-preserving masker consumes an unterminated block comment to
    # EOF, which otherwise makes the downstream parser report an opaque `*`
    # fragment or silently hide executable statements. Locate it explicitly so
    # a repair round receives the actual defect and source line.
    scan_index = 0
    while scan_index < len(content or ""):
        if content.startswith("--", scan_index):
            newline = content.find("\n", scan_index + 2)
            scan_index = len(content) if newline < 0 else newline + 1
            continue
        if content.startswith("/*", scan_index):
            comment_end = content.find("*/", scan_index + 2)
            if comment_end < 0:
                line_number = content.count("\n", 0, scan_index) + 1
                diagnostics.append(
                    f"line {line_number}: unterminated SQL block comment; add `*/` before "
                    "the next executable statement"
                )
                break
            scan_index = comment_end + 2
            continue
        if content[scan_index] == "'":
            scan_index += 1
            while scan_index < len(content):
                if content[scan_index] != "'":
                    scan_index += 1
                    continue
                if scan_index + 1 < len(content) and content[scan_index + 1] == "'":
                    scan_index += 2
                    continue
                scan_index += 1
                break
            continue
        scan_index += 1
    stored_declaration = re.compile(
        r"(?im)^\s*CREATE\s+OR\s+REPLACE\s+"
        r"(PROCEDURE|FUNCTION|TRIGGER)\s+"
        r"(\"?[A-Za-z][A-Za-z0-9_$#]*\"?)\b",
    )
    stored_units = list(stored_declaration.finditer(masked))
    all_unit_starts = sorted(
        [match.start() for match in units] + [match.start() for match in stored_units]
    )
    for stored in stored_units:
        kind = stored.group(1).casefold()
        raw_name = stored.group(2)
        name = raw_name.strip('"')
        next_start = next(
            (position for position in all_unit_starts if position > stored.start()),
            len(masked),
        )
        segment = masked[stored.end():next_start]
        named_end = re.search(
            rf"(?i)\bEND\s+{re.escape(raw_name)}\s*;", segment,
        )
        # Oracle permits an unnamed outer END. Accept it only at the actual
        # unit boundary (EOF or immediately before SQL*Plus `/`); otherwise an
        # inner BEGIN/END inside a truncated routine would be a false success.
        unnamed_end = re.search(
            r"(?is)\bEND\s*;\s*(?:/\s*)?$", segment,
        )
        if not named_end and not unnamed_end:
            line_number = masked.count("\n", 0, stored.start()) + 1
            diagnostics.append(
                f"line {line_number}: oracle PL/SQL {kind} {raw_name} is incomplete: "
                f"missing outer END {name}; before the next stored unit or end of file"
            )
    for insert_all in re.finditer(r"(?is)\bINSERT\s+ALL\b(.*?);", masked):
        block = insert_all.group(1)
        if not re.search(
            r"(?is)\bINTO\s+[A-Za-z][A-Za-z0-9_$#]*\s*\([^;]*?\)\s*VALUES\s*\(",
            block,
        ):
            diagnostics.append(
                "oracle INSERT ALL requires one or more `INTO table (columns) VALUES "
                "(values)` clauses followed by `SELECT 1 FROM DUAL`; SELECT expressions "
                "using `column => value` are not valid row constructors"
            )
    named_selects = list(re.finditer(
        r"(?im)^\s*SELECT\s+[^;\r\n]*=>[^;\r\n]*(?:FROM\s+DUAL)?",
        masked,
    ))
    for named_select in named_selects[:3]:
        line_number = masked.count("\n", 0, named_select.start()) + 1
        diagnostics.append(
            f"line {line_number}: Oracle SELECT cannot use PL/SQL named association "
            "syntax `column => value`; use an INTO ... VALUES clause or ordinary expressions"
        )
    if len(named_selects) > 3:
        diagnostics.append(
            f"{len(named_selects) - 3} additional Oracle SELECT statement(s) use invalid "
            "`column => value` named association syntax"
        )
    masked_lines = masked.splitlines()
    for index, line in enumerate(masked_lines):
        if line.strip() != "/":
            continue
        previous = next(
            (masked_lines[position].strip() for position in range(index - 1, -1, -1)
             if masked_lines[position].strip()),
            "",
        )
        if not re.search(r"(?i)\bEND(?:\s+[A-Za-z][A-Za-z0-9_$#]*)?\s*;\s*$", previous):
            diagnostics.append(
                f"line {index + 1}: SQL*Plus `/` execution separator follows ordinary SQL; "
                "use it only after a complete PL/SQL block, package, procedure, function, or trigger"
            )
    for invalid_table in re.finditer(
        r"(?im)^\s*CREATE\s+OR\s+REPLACE\s+TABLE\s+([A-Za-z][A-Za-z0-9_$#]*)\b",
        masked,
    ):
        diagnostics.append(
            f"oracle table {invalid_table.group(1)} uses unsupported CREATE OR REPLACE TABLE; "
            "use CREATE TABLE or an explicit migration block"
        )
    for view in re.finditer(
        r"(?is)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+)?VIEW\s+"
        r"([A-Za-z][A-Za-z0-9_$#]*)\b.*?\bAS\b(.*?);",
        masked,
    ):
        bind = re.search(r"(?<!:):([A-Za-z][A-Za-z0-9_$#]*)\b", view.group(2))
        if bind:
            diagnostics.append(
                f"oracle view {view.group(1)} contains bind variable :{bind.group(1)}; "
                "views cannot contain bind variables, so use a parameterized query or function"
            )
    seen: set[tuple[bool, str]] = set()
    for unit_index, match in enumerate(units):
        is_body = bool(match.group(1))
        raw_name = match.group(2)
        name = raw_name.strip('"').casefold()
        kind = "body" if is_body else "specification"
        key = (is_body, name)
        if key in seen:
            diagnostics.append(f"oracle PL/SQL package {kind} {raw_name} is declared more than once")
        seen.add(key)
        end_offset = units[unit_index + 1].start() if unit_index + 1 < len(units) else len(masked)
        segment = masked[match.end():end_offset]
        terminators = list(re.finditer(
            r"(?im)^\s*END(?:\s+(\"?[A-Za-z][A-Za-z0-9_$#]*\"?))?\s*;\s*$",
            segment,
        ))
        if not terminators:
            diagnostics.append(
                f"oracle PL/SQL package {kind} {raw_name} is missing its outer END statement"
            )
            continue
        outer_end = terminators[-1]
        end_name = (outer_end.group(1) or "").strip('"')
        if end_name and end_name.casefold() != name:
            diagnostics.append(
                f"oracle PL/SQL package {kind} {raw_name} ends as {end_name}; "
                f"use END {raw_name};"
            )
        if is_body:
            direct_ddl = re.search(
                r"(?im)^\s*(CREATE|ALTER|DROP|TRUNCATE)\s+"
                r"(?:TABLE|INDEX|SEQUENCE|VIEW|USER|ROLE|SCHEMA)\b",
                segment,
            )
            if direct_ddl:
                diagnostics.append(
                    f"oracle PL/SQL package body {raw_name} contains direct "
                    f"{direct_ddl.group(1).upper()} DDL; execute DDL through EXECUTE IMMEDIATE"
                )
    return diagnostics


# Function: _validate_sql
def _validate_sql(rel_path: str, content: str, dialect_hint: str) -> ValidationResult:
    configured_dialect = _resolve_sql_dialect(dialect_hint)
    inferred_dialect = _infer_sql_dialect(content)
    if configured_dialect and inferred_dialect and configured_dialect != inferred_dialect:
        return ValidationResult(
            rel_path, "sql", "compiler", False,
            [
                f"SQL dialect mismatch: target is {configured_dialect}, but generated "
                f"syntax is {inferred_dialect}. Regenerate using only the target dialect."
            ],
        )
    dialect = configured_dialect or inferred_dialect
    parser_content = (
        _normalize_oracle_sqlplus_commands(content)
        if dialect == "oracle" else content
    )
    if dialect == "oracle":
        oracle_diagnostics = _oracle_plsql_diagnostics(content)
        if oracle_diagnostics:
            return ValidationResult(
                rel_path, "sql", "compiler", False, oracle_diagnostics,
            )
    sqlfluff_dialect = _SQLFLUFF_DIALECTS.get(dialect)
    if sqlfluff_dialect and _SQLFLUFF_AVAILABLE:
        return _validate_sqlfluff_dialect(
            rel_path, parser_content, dialect or "ANSI", sqlfluff_dialect,
        )
    if dialect == "db2":
        return ValidationResult(
            rel_path, "sql", "missing-parser", False,
            ["IBM Db2 validation requires the SQLFluff Db2 dialect parser"],
        )
    if not _SQLGLOT_AVAILABLE:
        return ValidationResult(
            rel_path, "sql", "missing-parser", False,
            ["Required SQLGlot dialect parser is not installed in the Modernization backend environment"],
        )
    try:
        sqlglot.parse(parser_content, read=dialect or None)
        diagnostics = _sql_safety_diagnostics(content)
        return ValidationResult(
            rel_path, "sql", "compiler", not diagnostics, diagnostics,
        )
    except (_SqlParseError, _SqlTokenError) as exc:
        # sqlglot's message already includes a line/col-annotated snippet.
        first_line = str(exc).splitlines()[0] if str(exc) else "parse error"
        label = dialect or "ANSI/generic"
        return ValidationResult(
            rel_path, "sql", "compiler", False,
            [f"{label} parse error: {first_line}"],
        )


# Function: _validate_sqlfluff_dialect
def _validate_sqlfluff_dialect(
    rel_path: str, content: str, dialect_label: str, parser_dialect: str,
) -> ValidationResult:
    parsed = _SqlFluffLinter(dialect=parser_dialect).parse_string(content)
    diagnostics = [
        f"{dialect_label} parse error: {violation}"
        for violation in parsed.violations
        if violation.__class__.__name__ in {"SQLParseError", "SQLLexError"}
    ]
    diagnostics.extend(_sql_safety_diagnostics(content))
    return ValidationResult(
        rel_path, "sql", "compiler", not diagnostics, diagnostics[:50],
    )


# ─── Structural heuristic (generic/config files, and last-resort fallback when a ──
# ─── language's real checker — C#/TS/SQL — isn't available on this deployment) ──

_FENCE_RE = re.compile(r"```(?:\w+)?")
_PLACEHOLDER_RE = re.compile(
    r"(?im)^\s*(?://|#|<!--)?\s*(TODO|FIXME|generation failed|rest of file omitted)"
)
_LINE_COMMENT_RE = re.compile(r"(//|#).*?$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_STRING_RE = re.compile(r'"""(?:\\.|[^"\\])*"""|\'\'\'(?:\\.|[^\'\\])*\'\'\'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`(?:\\.|[^`\\])*`')


# Function: _strip_strings_and_comments
def _strip_strings_and_comments(content: str) -> str:
    text = _HTML_COMMENT_RE.sub("", content)
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _STRING_RE.sub('""', text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


# Function: _structural_heuristic
def _structural_heuristic(content: str) -> list[str]:
    issues: list[str] = []
    if not content or not content.strip():
        issues.append("file is empty")
        return issues

    if _FENCE_RE.search(content):
        issues.append("leftover markdown code fence in raw file")

    ph = _PLACEHOLDER_RE.search(content)
    if ph:
        issues.append(f"placeholder/incomplete content: {ph.group(1)!r}")

    stripped = _strip_strings_and_comments(content)
    for open_ch, close_ch, name in (("{", "}", "braces"), ("(", ")", "parens"), ("[", "]", "brackets")):
        depth = 0
        for ch in stripped:
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
        if depth != 0:
            issues.append(f"unbalanced {name} (net {depth:+d})")

    return issues


# Function: _heuristic_result
def _heuristic_result(rel_path: str, language: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    return ValidationResult(rel_path, language, "advisory", len(issues) == 0, issues)


def _validate_bicep(rel_path: str, content: str) -> ValidationResult:
    """Perform deterministic Bicep syntax-shape checks without misrouting to a host compiler.

    Full cross-file/type validation remains the responsibility of the Bicep CLI at the
    whole-project build gate.  This parser catches the generation failures that are safe
    to determine from one file: truncation, unbalanced delimiters, malformed declarations,
    Markdown leakage, and content that is not Bicep at all.
    """
    issues = _structural_heuristic(content)
    stripped = _strip_strings_and_comments(content)
    declaration = re.compile(
        r"(?m)^\s*(?:targetScope\s*=|(?:param|var|output|resource|module|type|import|metadata)\s+[A-Za-z_][\w-]*)"
    )
    if not declaration.search(stripped):
        issues.append("Bicep file contains no recognized declaration")
    for index, line in enumerate(stripped.splitlines(), 1):
        value = line.strip()
        if not value or value in {"{", "}"} or value.startswith("@"): 
            continue
        if (
            re.match(r"^(?:param|var|output|resource|module|type|import|metadata)\b", value)
            and not re.match(r"^[A-Za-z_][\w-]*\s*:", value)
        ):
            if not re.match(r"^(?:param|var|output|resource|module|type|import|metadata)\s+[A-Za-z_][\w-]*\b", value):
                issues.append(f"line {index}: malformed Bicep declaration")
    return ValidationResult(rel_path, "bicep", "parser", not issues, issues)


def _validate_vue_sfc(rel_path: str, content: str, tmp_dir: Path) -> ValidationResult:
    """Validate Vue SFC container structure and each embedded language.

    A .vue file is not TypeScript: feeding the complete container to tsc
    produces false TS/JSX failures. Validate its template, script, and style
    regions independently while the Vite/Vue project build remains the final
    cross-file compiler gate.
    """
    source = content or ""
    issues = _structural_heuristic(source)
    section_re = re.compile(
        r"(?is)<(?P<tag>template|script|style)\b(?P<attrs>[^>]*)>"
        r"(?P<body>.*?)</(?P=tag)\s*>",
    )
    sections = list(section_re.finditer(source))
    for tag in ("template", "script", "style"):
        openings = len(re.findall(fr"(?is)<{tag}\b[^>]*>", source))
        closings = len(re.findall(fr"(?is)</{tag}\s*>", source))
        if openings != closings:
            issues.append(f"Vue SFC has {openings} opening and {closings} closing <{tag}> tag(s)")
    templates = [item for item in sections if item.group("tag").casefold() == "template"]
    if len(templates) != 1:
        issues.append(f"Vue SFC requires exactly one <template> section; found {len(templates)}")
    elif templates:
        issues.extend(f"template: {item}" for item in _validate_html_document(templates[0].group("body")))
    scripts = [item for item in sections if item.group("tag").casefold() == "script"]
    if len(scripts) > 2:
        issues.append("Vue SFC supports at most one normal and one setup <script> section")
    for index, script in enumerate(scripts, 1):
        attrs = script.group("attrs") or ""
        is_typescript = bool(re.search(r"(?i)\blang\s*=\s*['\"](?:ts|typescript)['\"]", attrs))
        suffix = ".ts" if is_typescript else ".js"
        result = _validate_typescript(f"{Path(rel_path).stem}.vue-script-{index}{suffix}", script.group("body"), tmp_dir)
        issues.extend(f"script {index}: {diagnostic}" for diagnostic in result.diagnostics)
    for index, style in enumerate(
        (item for item in sections if item.group("tag").casefold() == "style"), 1,
    ):
        style_issues = _structural_heuristic(style.group("body"))
        issues.extend(f"style {index}: {diagnostic}" for diagnostic in style_issues)
    if not source.strip():
        issues.append("file is empty")
    return ValidationResult(rel_path, "vue", "parser", not issues, list(dict.fromkeys(issues))[:50])


def _validate_framework_artifact(
    rel_path: str, content: str, language: str,
) -> ValidationResult:
    """Validate framework-owned manifests without misrouting them as source."""
    source = content or ""
    issues = _structural_heuristic(source)
    normalized = rel_path.replace("\\", "/")
    if not source.strip():
        issues.append("file is empty")
    elif language == "cmake":
        if not re.search(r"(?im)^\s*cmake_minimum_required\s*\(", source):
            issues.append("CMakeLists.txt requires cmake_minimum_required(...)")
        if not re.search(r"(?im)^\s*project\s*\(", source):
            issues.append("CMakeLists.txt requires project(...)")
        for line_number, line in enumerate(source.splitlines(), 1):
            value = line.split("#", 1)[0].strip()
            if value and not re.match(r"^[A-Za-z_][\w]*\s*\(.*\)\s*$", value):
                issues.append(f"line {line_number}: malformed CMake command")
    elif language == "go-module":
        if not re.search(r"(?m)^\s*module\s+\S+\s*$", source):
            issues.append("go.mod requires a module directive")
        if not re.search(r"(?m)^\s*go\s+\d+(?:\.\d+){1,2}\s*$", source):
            issues.append("go.mod requires a valid go version directive")
    elif language == "dependency-lock":
        for line_number, line in enumerate(source.splitlines(), 1):
            if line.strip() and len(line.split()) < 3:
                issues.append(f"line {line_number}: malformed dependency checksum entry")
    elif language == "r-description":
        fields = {
            match.group(1).casefold()
            for match in re.finditer(r"(?m)^([A-Za-z][A-Za-z0-9./-]*):\s*\S", source)
        }
        for required in ("package", "version"):
            if required not in fields:
                issues.append(f"R DESCRIPTION requires {required.title()}: field")
    elif language == "play-routes":
        route_re = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/\S*\s+\S+")
        for line_number, line in enumerate(source.splitlines(), 1):
            value = line.strip()
            if value and not value.startswith("#") and not route_re.match(value):
                issues.append(f"line {line_number}: malformed Play route")
    elif language == "cabal":
        lowered = source.casefold()
        for field in ("cabal-version:", "name:", "version:"):
            if not re.search(fr"(?m)^\s*{re.escape(field)}", lowered):
                issues.append(f"Cabal manifest requires {field}")
        if not re.search(r"(?im)^\s*(?:library|executable|test-suite|benchmark)\b", source):
            issues.append("Cabal manifest requires a build component")
    elif language == "erlang-config":
        significant = [line.strip() for line in source.splitlines() if line.strip() and not line.lstrip().startswith("%")]
        if significant and not significant[-1].endswith("."):
            issues.append("Erlang configuration term must end with '.'")
        if not re.search(r"(?m)^\s*[\[{]", source):
            issues.append("Erlang configuration contains no term")
    elif language in {"gradle-build", "sbt-build"}:
        tokens = ("plugins", "dependencies", "rootProject", "scalaVersion", "addSbtPlugin", "libraryDependencies")
        if not any(token.casefold() in source.casefold() for token in tokens):
            issues.append(f"{language} contains no recognized build declaration")
    elif language == "prisma":
        for block in ("generator", "datasource"):
            if not re.search(fr"(?im)^\s*{block}\s+\w+\s*\{{", source):
                issues.append(f"Prisma schema requires a {block} block")
    elif language in {"razor-template", "jsp-template", "aspnet-template"}:
        markup = re.sub(r"(?is)<%.*?%>", "", source)
        markup = re.sub(r"(?m)^\s*@(?:model|page|using|inject|inherits|namespace|addTagHelper)\b[^\r\n]*", "", markup)
        markup = re.sub(r"(?is)@\{.*?\}", "", markup)
        html_issues = _validate_html_document(markup)
        issues.extend(f"markup: {diagnostic}" for diagnostic in html_issues)
    return ValidationResult(
        rel_path, language, "parser", not issues,
        [f"{normalized}: {item}" for item in list(dict.fromkeys(issues))[:50]],
    )


# Function: _validate_generic
def _validate_generic(rel_path: str, content: str) -> ValidationResult:
    """Use deterministic parsers for machine-readable files; advisory checks only for prose."""
    ext = Path(rel_path).suffix.lower()
    name = Path(rel_path).name.lower()
    try:
        if ext in {".json", ".lock"} or Path(rel_path).name.lower() in {"package.json", "global.json"}:
            parsed_json = json.loads(content)
            if "cloudformation" in rel_path.casefold() or (
                isinstance(parsed_json, dict) and "AWSTemplateFormatVersion" in parsed_json
            ):
                _require_cloudformation_resources(parsed_json)
            return ValidationResult(rel_path, "json", "parser", True, [])
        if ext in {".yaml", ".yml"}:
            if _looks_like_helm_template(rel_path, content):
                return _validate_helm_template(rel_path, content)
            documents = list(yaml.load_all(content, Loader=_TaggedYamlLoader))
            _validate_yaml_artifact(rel_path, documents)
            return ValidationResult(rel_path, "yaml", "parser", True, [])
        if ext == ".toml":
            tomllib.loads(content)
            return ValidationResult(rel_path, "toml", "parser", True, [])
        if ext in {".xml", ".svg", ".csproj", ".fsproj", ".vbproj", ".props", ".targets"}:
            ElementTree.fromstring(content)
            return ValidationResult(rel_path, "xml", "parser", True, [])
        if ext in {".graphql", ".gql", ".graphqls"}:
            from graphql import parse as parse_graphql
            parse_graphql(content)
            return ValidationResult(rel_path, "graphql", "parser", True, [])
        if ext in {".tf", ".tfvars", ".hcl"}:
            import hcl2
            hcl2.loads(content)
            return ValidationResult(rel_path, "hcl", "parser", True, [])
        if ext in {".bicep", ".bicepparam"}:
            return _validate_bicep(rel_path, content)
        if name == "dockerfile" or name.startswith("dockerfile."):
            return _validate_dockerfile(rel_path, content)
        if ext in {".md", ".markdown"}:
            return _validate_markdown(rel_path, content)
        if name.startswith(".env"):
            issues = _validate_key_value_lines(content, allow_colon=False)
            return ValidationResult(rel_path, "dotenv", "parser", not issues, issues)
        if ext == ".properties":
            issues = _validate_key_value_lines(content, allow_colon=True)
            return ValidationResult(rel_path, "properties", "parser", not issues, issues)
        if ext in {".ini", ".cfg"} or name == ".editorconfig":
            import configparser
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.read_string(content)
            return ValidationResult(rel_path, "ini", "parser", True, [])
        if name.endswith(".sln"):
            if "Microsoft Visual Studio Solution File" not in content:
                raise ValueError("invalid Visual Studio solution header")
            solution_folder_guid = "2150E333-8FDC-42A3-9474-1A3956D46DE8"
            for match in re.finditer(
                r'(?im)^Project\("\{([^}]+)\}"\)\s*=\s*"[^"]+",\s*"([^"]+)"',
                content,
            ):
                project_type, project_path = match.groups()
                if project_type.upper() == solution_folder_guid:
                    continue
                if Path(project_path.replace("\\", "/")).suffix.casefold() not in {
                    ".csproj", ".fsproj", ".vbproj", ".vcxproj", ".sqlproj", ".wixproj", ".shproj",
                }:
                    raise ValueError(
                        f"solution project entry must reference an MSBuild project, not {project_path!r}"
                    )
            return ValidationResult(rel_path, "solution", "parser", True, [])
        if name.startswith(("requirements", "constraints")) and ext == ".txt":
            from packaging.requirements import Requirement
            for line in content.splitlines():
                value = line.strip()
                if value and not value.startswith(("#", "-", "git+", "http://", "https://")):
                    Requirement(value)
            return ValidationResult(rel_path, "python-requirements", "parser", True, [])
        if name in {".gitignore", ".dockerignore", "license", "license.txt"}:
            if not content.strip():
                raise ValueError("file is empty")
            return ValidationResult(rel_path, "text", "parser", True, [])
        if ext in {".html", ".htm"}:
            issues = _validate_html_document(content)
            return ValidationResult(rel_path, "html", "parser", not issues, issues)
        if ext == ".razor":
            # Razor is compiled in project context; raw csc treats markup as
            # hundreds of bogus C# errors. Keep the per-file gate structural.
            issues = _structural_heuristic(content)
            return ValidationResult(rel_path, "razor", "parser", not issues, issues)
        if name == "makefile":
            issues = _structural_heuristic(content)
            if not re.search(r"(?m)^[A-Za-z0-9_.%/-]+\s*:(?!=)", content):
                issues.append("Makefile contains no target")
            return ValidationResult(rel_path, "makefile", "parser", not issues, issues)
        if ext == ".conf" and "nginx" in name:
            issues = _structural_heuristic(content)
            for index, line in enumerate(content.splitlines(), 1):
                value = line.split("#", 1)[0].strip()
                if value and not value.endswith((";", "{", "}")):
                    issues.append(f"line {index}: nginx directive must end with ';', '{{', or '}}'")
            return ValidationResult(rel_path, "nginx", "parser", not issues, issues)
        if ext in {".css", ".scss", ".sass", ".less"}:
            # Balanced delimiters plus a declaration-level check catches the
            # truncation/malformed-block defects relevant to generated styles.
            stripped = _strip_strings_and_comments(content)
            for block in re.findall(r"\{([^{}]*)\}", stripped, re.DOTALL):
                for declaration in (part.strip() for part in block.split(";") if part.strip()):
                    if not declaration.startswith("@") and ":" not in declaration:
                        raise ValueError(f"invalid CSS declaration: {declaration[:80]}")
            return ValidationResult(rel_path, "css", "parser", True, [])
    except Exception as exc:
        return ValidationResult(rel_path, "generic", "parser", False, [str(exc)])
    issues = _structural_heuristic(content)
    if issues:
        return ValidationResult(rel_path, "generic", "parser", False, issues)
    return ValidationResult(
        rel_path, "generic", "unsupported-validator", False,
        [f"No strict validator is registered for {Path(rel_path).name or rel_path}"],
    )


# Function: _validate_key_value_lines
def _validate_key_value_lines(content: str, allow_colon: bool) -> list[str]:
    issues = []
    for index, line in enumerate(content.splitlines(), 1):
        value = line.strip()
        if not value or value.startswith(("#", "!")):
            continue
        separator = "=" if "=" in value else (":" if allow_colon and ":" in value else "")
        if not separator or not value.split(separator, 1)[0].strip():
            issues.append(f"line {index}: expected a non-empty key and {('=' if not allow_colon else '= or :')} separator")
    return issues


# Function: _validate_html_document
def _validate_html_document(content: str) -> list[str]:
    from html.parser import HTMLParser

    void_elements = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    class BalancedHtmlParser(HTMLParser):
        # Function: __init__
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.stack = []
            self.issues = []

        # Function: handle_starttag
        def handle_starttag(self, tag, attrs):
            if tag not in void_elements:
                self.stack.append(tag)

        # Function: handle_startendtag
        def handle_startendtag(self, tag, attrs):
            return None

        # Function: handle_endtag
        def handle_endtag(self, tag):
            if tag in void_elements:
                return
            if not self.stack or self.stack[-1] != tag:
                self.issues.append(f"unexpected closing tag </{tag}>")
                return
            self.stack.pop()

    parser = BalancedHtmlParser()
    try:
        parser.feed(content)
        parser.close()
    except Exception as exc:
        return [str(exc)]
    parser.issues.extend(f"unclosed tag <{tag}>" for tag in reversed(parser.stack))
    if not content.strip():
        parser.issues.append("file is empty")
    return parser.issues


# Function: _require_cloudformation_resources
def _require_cloudformation_resources(document) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("Resources"), dict):
        raise ValueError("CloudFormation template must contain a Resources mapping")


# Function: _validate_yaml_artifact
def _validate_yaml_artifact(rel_path: str, documents: list[object]) -> None:
    normalized = rel_path.replace("\\", "/").casefold()
    for document in documents:
        if isinstance(document, dict) and {"apiVersion", "kind"} <= set(document):
            metadata = document.get("metadata")
            if document.get("kind") != "List" and (
                not isinstance(metadata, dict) or not metadata.get("name")
            ):
                raise ValueError("Kubernetes manifest requires metadata.name")
        if isinstance(document, dict) and (
            "AWSTemplateFormatVersion" in document or "cloudformation" in normalized
        ):
            _require_cloudformation_resources(document)
    first = documents[0] if documents else None
    if "/.github/workflows/" in f"/{normalized}" or normalized.startswith(".github/workflows/"):
        if not isinstance(first, dict) or "jobs" not in first or not (
            "on" in first or True in first  # PyYAML 1.1 treats the key `on` as boolean.
        ):
            raise ValueError("GitHub Actions workflow requires on and jobs mappings")
    if Path(rel_path).name.casefold() == "chart.yaml":
        required = {"apiVersion", "name", "version"}
        if not isinstance(first, dict) or not required <= set(first):
            raise ValueError("Helm Chart.yaml requires apiVersion, name, and version")


# Function: _looks_like_helm_template
def _looks_like_helm_template(rel_path: str, content: str) -> bool:
    normalized = rel_path.replace("\\", "/").casefold()
    return "/templates/" in f"/{normalized}" and "{{" in content


# Function: _validate_helm_template
def _validate_helm_template(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content.replace("{{", "").replace("}}", ""))
    if content.count("{{") != content.count("}}"):
        issues.append("unbalanced Helm template actions")
    return ValidationResult(rel_path, "helm", "parser", not issues, issues)


# Function: _validate_markdown
def _validate_markdown(rel_path: str, content: str) -> ValidationResult:
    issues = []
    if not content.strip():
        issues.append("file is empty")
    fence_count = sum(1 for line in content.splitlines() if line.lstrip().startswith("```"))
    if fence_count % 2:
        issues.append("unclosed Markdown code fence")
    return ValidationResult(rel_path, "markdown", "parser", not issues, issues)


_DOCKERFILE_INSTRUCTIONS = {
    "ADD", "ARG", "CMD", "COPY", "ENTRYPOINT", "ENV", "EXPOSE", "FROM",
    "HEALTHCHECK", "LABEL", "MAINTAINER", "ONBUILD", "RUN", "SHELL",
    "STOPSIGNAL", "USER", "VOLUME", "WORKDIR",
}


# ─── Legacy/vendor-platform languages (heuristic, no real compiler available) ──

# Function: _balanced_keyword_pairs
def _balanced_keyword_pairs(logical: str, pairs: list[tuple[str, str]]) -> list[str]:
    """Count-based open/close keyword balance check (case-insensitive, word-
    boundary). Not a real parser - these languages have no lightweight open-
    source grammar available - but catches the single most common defect
    class in generated legacy code: a block opened and never closed (or
    closed once too often)."""
    issues = []
    for open_kw, close_kw in pairs:
        # A hyphenated close like "END-IF" contains "IF" with a genuine word
        # boundary right before it (the hyphen is a non-word char), so a naive
        # \bIF\b also matches inside "END-IF" itself - exclude anything
        # immediately preceded by "END-" or "END " to avoid double-counting
        # every close as an extra open. Harmless no-op for closes that don't
        # take this shape (e.g. ABAP's concatenated ENDIF has no \b there at all).
        opens = len(re.findall(rf"(?i)(?<!END-)(?<!END )\b{re.escape(open_kw)}\b", logical))
        closes = len(re.findall(rf"(?i)\b{re.escape(close_kw)}\b", logical))
        if opens != closes:
            issues.append(f"unbalanced {open_kw}/{close_kw} (opened {opens}x, closed {closes}x)")
    return issues


# Function: _validate_abap
def _validate_abap(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    if not re.search(r"(?im)^\s*(?:REPORT\b|CLASS\s+\w+|FUNCTION\s+\w+|FORM\s+\w+)", content):
        issues.append("ABAP source must declare a REPORT, CLASS, FUNCTION, or FORM")
    issues.extend(_balanced_keyword_pairs(content, [
        ("IF", "ENDIF"), ("LOOP", "ENDLOOP"), ("DO", "ENDDO"), ("CASE", "ENDCASE"),
        ("METHOD", "ENDMETHOD"), ("CLASS", "ENDCLASS"), ("FORM", "ENDFORM"),
        ("TRY", "ENDTRY"), ("WHILE", "ENDWHILE"),
    ]))
    return ValidationResult(rel_path, "abap", "heuristic", not issues, issues[:50])


# Function: _validate_rpg
def _validate_rpg(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    is_free = bool(re.search(r"(?im)^\s*\*\*FREE\b", content))
    if not is_free and not re.search(r"(?m)^.{5}[HFDCO]", content):
        issues.append("RPG source must be **FREE form or use fixed-form H/F/D/C/O spec columns")
    issues.extend(_balanced_keyword_pairs(content, [
        ("IF", "ENDIF"), ("SELECT", "ENDSL"),
        ("BEGSR", "ENDSR"), ("FOR", "ENDFOR"), ("MONITOR", "ENDMON"),
    ]))
    # DOW and DOU are two distinct openers that share one closer (ENDDO) - must
    # be counted together, same reasoning as Progress 4GL's DO/PROCEDURE vs END.
    dow_dou_opens = len(re.findall(r"(?i)\bDO[WU]\b", content))
    enddo_closes = len(re.findall(r"(?i)\bENDDO\b", content))
    if dow_dou_opens != enddo_closes:
        issues.append(f"unbalanced DOW/DOU vs ENDDO (opened {dow_dou_opens}x, closed {enddo_closes}x)")
    # Fixed-form: "SubName    BEGSR" (name precedes keyword, factor-1 column).
    # Free-form:  "Begsr SubName;" (keyword precedes name).
    subroutines = {name.upper() for name in re.findall(r"(?im)^\s*(\w+)\s+BEGSR\b", content)}
    subroutines |= {name.upper() for name in re.findall(r"(?i)\bBEGSR\s+(\w+)", content)}
    called = {name.upper() for name in re.findall(r"(?i)\bEXSR\s+(\w+)", content)}
    for name in sorted(called - subroutines):
        issues.append(f"EXSR target {name} has no matching BEGSR")
    return ValidationResult(rel_path, "rpg", "heuristic", not issues, issues[:50])


# Function: _validate_jcl
def _validate_jcl(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    lines = [line for line in content.splitlines() if line.strip()]
    if not any(re.match(r"^//\S+\s+JOB\b", line) for line in lines):
        issues.append("JCL must contain exactly one //jobname JOB statement")
    if not any(re.match(r"^//\S+\s+EXEC\b", line) for line in lines):
        issues.append("JCL must contain at least one //stepname EXEC statement")
    for index, line in enumerate(lines, 1):
        if not (line.startswith(("//", "/*"))):
            issues.append(f"line {index}: JCL statement must start with '//'")
    return ValidationResult(rel_path, "jcl", "heuristic", not issues, issues[:50])


# Function: _validate_mumps
def _validate_mumps(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    commands = re.findall(r"(?im)^\s*\.*\s*(S|SET|I|IF|F|FOR|D|DO|Q|QUIT|N|NEW|W|WRITE)\b", content)
    if not commands:
        issues.append("no recognized MUMPS commands found (SET/IF/FOR/DO/QUIT/NEW/WRITE)")
    for line_no, line in enumerate(content.splitlines(), 1):
        label_match = re.match(r"^([A-Za-z%][A-Za-z0-9]*)(\(.*\))?\s", line)
        if label_match and " " in label_match.group(1):
            issues.append(f"line {line_no}: MUMPS label must not contain spaces")
    return ValidationResult(rel_path, "mumps", "heuristic", not issues, issues[:50])


# Function: _validate_natural
def _validate_natural(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    if not re.search(r"(?i)\bDEFINE\s+DATA\b", content):
        issues.append("Natural source must declare DEFINE DATA")
    issues.extend(_balanced_keyword_pairs(content, [
        ("DEFINE DATA", "END-DEFINE"), ("IF", "END-IF"), ("DECIDE", "END-DECIDE"),
        ("REPEAT", "END-REPEAT"), ("FOR", "END-FOR"), ("SUBROUTINE", "END-SUBROUTINE"),
    ]))
    return ValidationResult(rel_path, "natural", "heuristic", not issues, issues[:50])


# Function: _validate_progress4gl
def _validate_progress4gl(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    if not re.search(r"(?i)\bDEFINE\s+(?:VARIABLE|TEMP-TABLE|BUFFER|INPUT|OUTPUT)\b", content):
        issues.append("Progress 4GL/ABL source must contain at least one DEFINE statement")
    # DO and PROCEDURE blocks share the same closing keyword (END), so they must
    # be counted together - counting them as two independent pairs against the
    # same END occurrences would flag a false imbalance whenever the DO count
    # and PROCEDURE count simply differ, which is the normal case.
    opens = len(re.findall(r"(?i)\b(?:DO|PROCEDURE)\b", content))
    closes = len(re.findall(r"(?i)\bEND\b", content))
    if opens != closes:
        issues.append(f"unbalanced DO/PROCEDURE vs END (opened {opens}x, closed {closes}x)")
    return ValidationResult(rel_path, "progress4gl", "heuristic", not issues, issues[:50])


# Function: _validate_apex
def _validate_apex(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    has_class = re.search(
        r"(?im)^\s*(?:public|private|global)?\s*(?:with\s+sharing\s+|without\s+sharing\s+)?(?:class|interface)\s+\w+",
        content,
    )
    has_trigger = re.search(r"(?im)^\s*trigger\s+\w+\s+on\s+\w+", content)
    if not (has_class or has_trigger):
        issues.append("Apex source must declare a class, interface, or trigger")
    return ValidationResult(rel_path, "apex", "heuristic", not issues, issues[:50])


# Function: _validate_pli
def _validate_pli(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    if not re.search(r"(?im)^\s*\w+\s*:\s*PROCEDURE\b", content):
        issues.append("PL/I source must declare a labeled PROCEDURE")
    # DO and PROCEDURE blocks share the same closing keyword (END) - must be
    # counted together, same reasoning as Progress 4GL's DO/PROCEDURE vs END.
    opens = len(re.findall(r"(?i)\b(?:DO|PROCEDURE)\b", content))
    closes = len(re.findall(r"(?i)\bEND\b", content))
    if opens != closes:
        issues.append(f"unbalanced DO/PROCEDURE vs END (opened {opens}x, closed {closes}x)")
    return ValidationResult(rel_path, "pli", "heuristic", not issues, issues[:50])


_LEGACY_HEURISTIC_VALIDATORS = {
    "abap": _validate_abap,
    "rpg": _validate_rpg,
    "jcl": _validate_jcl,
    "mumps": _validate_mumps,
    "natural": _validate_natural,
    "progress4gl": _validate_progress4gl,
    "apex": _validate_apex,
    "pli": _validate_pli,
}


# Function: _validate_jenkinsfile
def _validate_jenkinsfile(rel_path: str, content: str) -> ValidationResult:
    """Structural check only - no Groovy/Jenkins-pipeline grammar is available as a
    dependency here. Verifies the declarative-pipeline shape (or a scripted node{}
    block) rather than full Groovy semantics; same honesty tier as _validate_dockerfile."""
    issues = _structural_heuristic(content)
    if re.search(r"(?m)^\s*pipeline\s*\{", content):
        if not re.search(r"(?m)^\s*agent\b", content):
            issues.append("declarative pipeline must define an 'agent'")
        if not re.search(r"(?m)^\s*stages\s*\{", content):
            issues.append("declarative pipeline must define a 'stages' block")
        if not re.search(r"(?m)^\s*stage\s*\(", content):
            issues.append("declarative pipeline must define at least one stage(...)")
    elif not re.search(r"\bnode\s*(?:\(|\{)", content):
        issues.append(
            "Jenkinsfile must contain either a declarative 'pipeline { }' block "
            "or a scripted 'node { }' block"
        )
    return ValidationResult(rel_path, "jenkinsfile", "heuristic", not issues, issues[:50])


# Function: _validate_dockerfile
def _validate_dockerfile(rel_path: str, content: str) -> ValidationResult:
    issues = _structural_heuristic(content)
    logical_lines = []
    pending = ""
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending += stripped
        if pending.endswith("\\"):
            pending = pending[:-1] + " "
            continue
        logical_lines.append(pending)
        pending = ""
    if pending:
        issues.append("unterminated Dockerfile line continuation")
    for index, line in enumerate(logical_lines, 1):
        instruction = line.split(maxsplit=1)[0].upper()
        if instruction not in _DOCKERFILE_INSTRUCTIONS:
            issues.append(f"logical line {index}: unknown Dockerfile instruction {instruction}")
    if not any(line.upper().startswith("FROM ") for line in logical_lines):
        issues.append("Dockerfile must contain a FROM instruction")
    return ValidationResult(rel_path, "dockerfile", "parser", not issues, issues[:50])
