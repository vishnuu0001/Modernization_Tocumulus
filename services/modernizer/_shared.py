# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — services/modernizer (_shared.py)
# Date: 2025-11-18
# ---------------------------------------------------------------------------
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Keep validated generation results on the application's persistent data
# volume when configured.  The host-side default remains isolated in the OS
# temporary directory, while Docker deployments point this at /app/data so an
# application-only upgrade or restart can resume without regenerating files.
_LLM_CACHE_DIR = Path(
    os.getenv("MODERNIZATION_LLM_CACHE_DIR")
    or (Path(tempfile.gettempdir()) / "modernization_llm_cache")
)
try:
    _LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# Domain-level generation cache — stores entire _llm_gen_domain() output dict
_DOM_CACHE_DIR = _LLM_CACHE_DIR / "domains"
try:
    _DOM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
# TTL for domain cache entries (default 24 h, override with env var)
_DOM_CACHE_TTL = int(os.getenv("MODERNIZATION_DOM_CACHE_TTL", str(24 * 3600)))

_DEFAULT_EXT_FOR_LANG = {
    "python": ".py", "csharp": ".cs", "java": ".java", "typescript": ".ts", "sql": ".sql",
    "javascript": ".js", "c": ".c", "cpp": ".cpp", "cobol": ".cob",
    "php": ".php", "ruby": ".rb", "go": ".go",
    "rust": ".rs", "swift": ".swift", "kotlin": ".kt", "shell": ".sh",
    "r": ".r", "scala": ".scala", "clojure": ".clj", "haskell": ".hs",
    "lisp": ".lisp", "elixir": ".ex", "dart": ".dart", "julia": ".jl",
    "hcl": ".tf", "protobuf": ".proto", "fortran": ".f90", "ada": ".adb",
    "pascal": ".pas", "erlang": ".erl", "ocaml": ".ml", "prolog": ".pl",
    "abap": ".abap", "pli": ".pli", "rpg": ".rpgle", "jcl": ".jcl",
    "mumps": ".m", "natural": ".nsp", "progress4gl": ".p", "apex": ".cls",
    "yaml": ".yaml", "json": ".json", "toml": ".toml", "xml": ".xml", "svg": ".svg",
    "graphql": ".graphql", "dockerfile": ".dockerfile",
    "cloudformation": ".yaml", "kubernetes": ".yaml", "helm": ".yaml",
    "ansible": ".yaml", "github_actions": ".yaml", "jenkinsfile": ".jenkinsfile",
    "markdown": ".md",
}

# ─── Code-generation constants (replaces scattered magic numbers) ─────────────

# User-facing label for progress messages like "LLM (devstral-small-2:24b): generating
# single complete file…" — shown as "LLM (Open Source)" instead of the raw
# Ollama model tag. This is cosmetic only: `llm_model` itself (the actual
# string passed to services.llm.generate()) is completely untouched
# everywhere else — only the text built for progress()/on_step() messages
# uses this label.
_LLM_DISPLAY_LABEL = "Open Source"

# LLM token budgets
_TOKENS_DEFAULT       = 4096
_TOKENS_LARGE         = 8192
_TOKENS_XLARGE        = 12_000
_TOKENS_COMPONENT     = 6_000
_TOKENS_MIGRATION     = 6_144

# Adaptive context window: chars-per-token estimate and safety margin
_CTX_SIZES            = (4096, 8192, 16_384, 32_768)
_CTX_SAFETY_MARGIN    = 512
_CTX_CHARS_PER_TOKEN  = 3

# Source content / reference-file size limits
_SRC_MAX_CHARS        = 14_000
_SRC_TRUNCATE_AT      = 12_000
_REF_FILE_MAX_CHARS   = 2_000
_REF_FILE_MIN_BYTES   = 300
_REF_FILE_MAX_BYTES   = 2_500

# Structure-extraction / string limits
_MAX_STRUCT_ITEMS     = 60
_MAX_STACK_NAME_LEN   = 120
_STRUCT_LINE_LONG     = 110
_STRUCT_LINE_SHORT    = 80
_STRUCT_LINE_MED      = 100

# Pagination / generation defaults
_DEFAULT_PAGE_SIZE    = 20
# Not read anywhere — conversion_pipeline.py resolves the actual domain-worker
# default inline (see its worker_default local, "1" for every language: a
# single-GPU Ollama serializes inference regardless, so concurrent domain
# calls just queue and time themselves out). Kept in sync so a future reader
# of this constant isn't misled into thinking a stale "5" is still live.
try:
    _DEFAULT_ASYNC_BATCH_WORKERS = max(
        1, min(4, int(os.getenv("MODERNIZATION_ASYNC_BATCH_WORKERS", "2")))
    )
except ValueError:
    _DEFAULT_ASYNC_BATCH_WORKERS = 1
_DEFAULT_DOM_WORKERS  = _DEFAULT_ASYNC_BATCH_WORKERS
_DEFAULT_FILE_WORKERS = _DEFAULT_ASYNC_BATCH_WORKERS
_MAX_PLAN_FILES       = 18
_PLAN_PROMPT_MAX_TOKENS = 800
_EXPLICIT_MANIFEST_LIMIT = 160

# Legitimate filenames with no extension — allow-listed so the file-plan
# parser's "must have an extension" check (added to reject bare section
# headers like a lone "/Backend" line) doesn't also reject these.
_EXTENSIONLESS_FILENAMES = {
    "Dockerfile", "Makefile", "Procfile", "LICENSE", "Jenkinsfile",
    "Vagrantfile", "Rakefile", "Gemfile",
}

# How often (in streamed tokens) a streaming generate() call reports
# incremental progress — high enough to avoid flooding the SSE queue,
# low enough that the bar visibly moves during a multi-minute call.
_STREAM_PROGRESS_EVERY_TOKENS = 8

# Per-call wall-clock ceiling for targeted single-file repair/closure LLM
# calls (compiler-error repair, cross-module boundary rewrites, missing-
# contract closure files) — deliberately smaller than a full first-draft
# generation, since these calls rewrite one already-modest file rather than
# author a new project from scratch. Env-overridable for slower hardware.
_REPAIR_CALL_MAX_SECONDS = float(os.getenv("MODERNIZATION_REPAIR_CALL_MAX_SECONDS", "300"))

# One wall-clock envelope for the complete planning phase, including retries
# with larger token budgets.  Planning is advisory and has a validated,
# deterministic fallback, so it must never be able to strand an otherwise
# viable generation job indefinitely.
_PLANNING_TOTAL_BUDGET_SECONDS = max(
    60.0, float(os.getenv("MODERNIZATION_PLANNING_TOTAL_BUDGET_SECONDS", "1800"))
)

# Aggregate wall-clock budget for a generated non-Java file, including syntax
# repair attempts. Streaming HTTP clients reset their read timeout whenever a
# token arrives, so transport timeouts alone do not stop an extremely slow
# generation that continues to emit occasional tokens. Thirty minutes is a
# generous ceiling for one source file on the supported local Devstral host
# while still guaranteeing forward progress.
_FILE_GENERATION_MAX_SECONDS = float(
    os.getenv("MODERNIZATION_FILE_GENERATION_MAX_SECONDS", "1800")
)

# Interactive single-file mode gets one bounded monolithic draft. Oversized,
# truncated, or slow drafts then switch to skeleton/member generation instead
# of consuming the generic 30-minute project-file budget and failing.
_SINGLE_FILE_DRAFT_MAX_SECONDS = max(
    60.0, float(os.getenv("MODERNIZATION_SINGLE_FILE_DRAFT_MAX_SECONDS", "600"))
)
_SINGLE_FILE_STRUCTURAL_CALL_MAX_SECONDS = max(
    60.0, float(os.getenv("MODERNIZATION_SINGLE_FILE_STRUCTURAL_CALL_MAX_SECONDS", "600"))
)
_SINGLE_FILE_SINGLE_SHOT_TOKEN_THRESHOLD = max(
    512, int(os.getenv("MODERNIZATION_SINGLE_FILE_SINGLE_SHOT_TOKEN_THRESHOLD", "2400"))
)
# Upper bound (not a target) for one single-file draft including its governed
# envelope. Prose prompts have no enumerated members, so the contract estimate
# alone under-sizes ordinary modules and causes a token-limit cutoff.
_SINGLE_FILE_MIN_OUTPUT_TOKENS = max(
    2048, int(os.getenv("MODERNIZATION_SINGLE_FILE_MIN_OUTPUT_TOKENS", "6144"))
)
# A short, unenumerated script gets a smaller allowance and explicit concise
# prompt. This is still large enough for a complete standalone source file;
# token-limit truncation remains fail-closed in services.llm.
_SINGLE_FILE_SIMPLE_OUTPUT_TOKENS = max(
    1024, int(os.getenv("MODERNIZATION_SINGLE_FILE_SIMPLE_OUTPUT_TOKENS", "4096"))
)
# Wall-clock bound for the notes-only accountability call that follows a
# validated single file whose inline governance notes were missing/invalid.
_SINGLE_FILE_NOTES_MAX_SECONDS = max(
    60.0, float(os.getenv("MODERNIZATION_SINGLE_FILE_NOTES_MAX_SECONDS", "420"))
)

# One aggregate wall-clock budget for a Java file's initial draft plus any
# syntax-repair attempts.  This is intentionally Java-specific: the regression
# came from Java files multiplying the transport retry ceiling by the validator
# retry count (up to 3 x 3 calls), while other language pipelines retain their
# established timeout behavior.
# 300s previously left no margin at all: on observed hardware, a single
# "COMPLETE, PRODUCTION-READY" Controller/Entity draft (full CRUD, validated
# DTOs, exception mapper) alone regularly took 300-600s, so the *first*
# attempt — before any repair round even started — already blew the whole
# aggregate budget, guaranteeing "Java domain generation did not produce
# validated complete artifacts" regardless of whether the model's output was
# actually any good. 1800s gives the supported 24B default real headroom for
# at least one repair attempt. Env-overridable for faster/slower hardware.
_JAVA_FILE_GENERATION_MAX_SECONDS = float(
    os.getenv("MODERNIZATION_JAVA_FILE_GENERATION_MAX_SECONDS", "1800")
)

# File-by-file modernization is followed by strict whole-project compilation
# and compiler-diagnostic repair for every supported project target. Re-running
# the model up to three times while validating one file in isolation duplicates
# that later repair work and scales poorly with large source inventories.
_CONVERSION_MAX_ATTEMPTS = max(
    1, int(os.getenv("MODERNIZATION_CONVERSION_MAX_ATTEMPTS", "1"))
)
_CONVERSION_FILE_MAX_SECONDS = max(
    60.0, float(os.getenv("MODERNIZATION_CONVERSION_FILE_MAX_SECONDS", "600"))
)

# Retain Java-specific overrides for operators who need different limits. The
# supported 24B model produced 2,500-3,600-token Java conversions at roughly
# 3-8 tokens/second on the reference host; inheriting the generic 600-second
# ceiling cut otherwise healthy streams near completion. Give Java a bounded
# 20-minute default while other languages retain the 10-minute policy.
_JAVA_CONVERSION_MAX_ATTEMPTS = max(
    1, int(os.getenv(
        "MODERNIZATION_JAVA_CONVERSION_MAX_ATTEMPTS",
        str(_CONVERSION_MAX_ATTEMPTS),
    ))
)
_JAVA_CONVERSION_FILE_MAX_SECONDS = max(
    60.0, float(os.getenv(
        "MODERNIZATION_JAVA_CONVERSION_FILE_MAX_SECONDS",
        str(max(_CONVERSION_FILE_MAX_SECONDS, 1200.0)),
    ))
)

# Configured minimum budget for an initial per-file generation wave. Individual
# calls are bounded, and the effective budget below scales with worker batches:
# generous enough to never trip on a real, working generation, but
# still finite so the round — and the job — always reaches closure even if
# one file's call genuinely never returns.
_WAVE_ROUND_BUDGET_SECONDS = float(os.getenv("MODERNIZATION_WAVE_ROUND_BUDGET_SECONDS", "1800"))


def _configured_batch_workers(item_count: int, *env_keys: str, max_workers: int = 4) -> int:
    """Resolve a bounded async width from specific to deployment-wide keys."""
    raw = None
    for key in env_keys:
        value = os.getenv(key)
        if value is not None and str(value).strip():
            raw = value
            break
    if raw is None:
        deployment_width = os.getenv("MODERNIZATION_ASYNC_BATCH_WORKERS")
        if deployment_width is None:
            from services.llm_runtime import worker_width
            return worker_width(item_count, max_workers=max_workers)
        raw = deployment_width
    try:
        configured = int(raw)
    except (TypeError, ValueError):
        configured = 1
    return max(1, min(max_workers, item_count or 1, configured))


# Function: _round_budget_seconds
def _round_budget_seconds(item_count: int, workers: int, call_budget_seconds: float,
                           margin_seconds: float = 60.0) -> float:
    """Worst-case wall-clock budget for a parallel round of bounded LLM calls.

    `workers` process `item_count` items in ceil(item_count / workers)
    sequential batches; if every call in the round took the full per-call
    ceiling, the round would take that many batches times the ceiling. Adding
    a fixed margin absorbs prompt-build/progress overhead so the round budget
    itself is never the tight constraint — `call_budget_seconds` already is.
    """
    if item_count <= 0 or workers <= 0:
        return margin_seconds
    batches = -(-item_count // workers)  # ceil division without importing math
    return batches * call_budget_seconds + margin_seconds


# Function: _generation_wave_budget_seconds
def _generation_wave_budget_seconds(item_count: int, workers: int, language: str) -> float:
    """Give every sequential worker batch a complete per-file call window.

    The configured wave value is a minimum safety floor. It must not become a
    premature ceiling for files that are still queued behind earlier batches.
    """
    per_file_budget = (
        _JAVA_FILE_GENERATION_MAX_SECONDS
        if language.casefold() == "java" else _FILE_GENERATION_MAX_SECONDS
    )
    return max(
        _WAVE_ROUND_BUDGET_SECONDS,
        _round_budget_seconds(item_count, workers, per_file_budget),
    )


# Function: _run_bounded_round
def _run_bounded_round(
    executor, futures: dict, *, round_budget_seconds: float, label: str,
    activity_by_key: dict | None = None,
):
    """Wait for a parallel batch of futures without ever blocking indefinitely.

    A ThreadPoolExecutor used as a context manager joins every submitted
    thread on `__exit__`, and `as_completed()` with no timeout waits forever
    for the slowest future. Together those mean a single LLM call that hangs
    — or is merely far slower than the rest of the batch — blocks the entire
    round, and therefore the whole generation job, from ever reaching
    completion. Without activity tracking, this bounds the absolute wait.
    With ``activity_by_key``, the same value is a stall budget: a worker may
    outlive it while progress heartbeats continue, but a silent worker is
    abandoned (its thread may still be running in the background — Python
    cannot forcibly kill it) so the caller can still close out.

    `futures` maps each submitted future to a caller-defined key (typically a
    file path). Returns `(done, timed_out)`:
      - `done`: the subset of `futures` that finished within budget. Callers
        still call `.result()` on each to get the value or raised exception.
      - `timed_out`: {key: message} for every future abandoned past budget.
    """
    from concurrent.futures import FIRST_COMPLETED, wait as _wait

    budget = max(0.01, round_budget_seconds)
    if activity_by_key is None:
        done, not_done = _wait(futures, timeout=budget)
    else:
        # A project-file worker may contain several independently bounded LLM
        # calls (structural members plus validation repairs). Treat the round
        # budget as a stall limit, not an absolute lifetime which races and
        # abandons a worker that is still streaming useful progress.
        started = time.monotonic()
        pending = set(futures)
        done = set()
        not_done = set()
        while pending:
            now = time.monotonic()
            stalled = {
                future for future in pending
                if now - float(activity_by_key.get(futures[future], started)) >= budget
            }
            if stalled:
                not_done.update(stalled)
                pending.difference_update(stalled)
                continue
            completed, _ = _wait(
                pending, timeout=min(5.0, budget), return_when=FIRST_COMPLETED,
            )
            done.update(completed)
            pending.difference_update(completed)
    # shutdown(wait=False) does not block on stragglers still running in the
    # executor's threads — only on submitting no further work to it.
    executor.shutdown(wait=False, cancel_futures=True)
    timed_out = {}
    if not_done:
        keys = sorted(str(futures[f]) for f in not_done)
        logger.error(
            "%s: %d/%d worker(s) were idle for the %.0fs round budget and were "
            "abandoned so the job can still reach completion: %s",
            label, len(not_done), len(futures), round_budget_seconds,
            ", ".join(keys[:8]),
        )
        for f in not_done:
            timed_out[futures[f]] = (
                f"{label} made no progress for the {round_budget_seconds:.0f}s round budget "
                "and was abandoned; prior content was kept"
            )
    return {f: futures[f] for f in done}, timed_out


# Function: _streaming_progress_cb
def _streaming_progress_cb(progress_fn, phase: str, pct_start: int, pct_end: int,
                            max_tokens: int, label: str):
    """Build an on_token callback that moves the progress bar smoothly from
    pct_start to pct_end as a single LLM call streams its response, instead
    of the bar sitting frozen at pct_start for the entire call.

    Interpolates by estimated output size (chars generated so far vs. the
    call's max_tokens budget, using the codebase's own ~3-chars/token
    estimate) — an approximation, since the model may stop well short of
    max_tokens, but it turns a frozen bar into a moving one.
    """
    state = {"chars": 0, "tokens": 0}
    target_chars = max(max_tokens * _CTX_CHARS_PER_TOKEN, 1)

    # Function: _on_token
    def _on_token(token: str) -> None:
        state["chars"] += len(token)
        state["tokens"] += 1
        if state["tokens"] % _STREAM_PROGRESS_EVERY_TOKENS != 0:
            return
        frac = min(0.97, state["chars"] / target_chars)  # reserve the last stretch for completion
        pct = pct_start + int(frac * (pct_end - pct_start))
        estimated_tokens = max(1, state["chars"] // _CTX_CHARS_PER_TOKEN)
        progress_fn(
            phase,
            min(max(pct, pct_start + 1), pct_end),
            f"{label} ({estimated_tokens:,} tokens generated)",
        )

    # services.llm detects this optional hook and reports transport retries
    # immediately.  Without it, a slow retry emits no tokens and the browser
    # appears frozen at the last completed-file percentage for several minutes.
    def _on_retry(attempt: int, total: int, error: str) -> None:
        reason = "timeout" if "timed out" in error.casefold() else "transient Ollama error"
        progress_fn(
            phase,
            max(pct_start, min(pct_end, pct_start + 1)),
            f"{label} — retrying inference {attempt}/{total} after {reason}",
        )

    _on_token.on_retry = _on_retry

    return _on_token


# ─── Performance helpers ──────────────────────────────────────────────────────

# Function: _adaptive_num_ctx
def _adaptive_num_ctx(prompt_chars: int, max_output_tokens: int = _TOKENS_DEFAULT) -> int:
    """
    Return the smallest Ollama context window that fits the request.
    Smaller windows load the KV cache faster and speed up generation.
    Rule of thumb: ~3 chars per token in source code.
    """
    needed = (prompt_chars // _CTX_CHARS_PER_TOKEN) + max_output_tokens + _CTX_SAFETY_MARGIN
    for ctx in _CTX_SIZES:
        if ctx >= needed:
            return ctx
    return 32768


# Function: _adaptive_max_tokens
def _adaptive_max_tokens(src_content: str, src_lang: str, tgt_lang: str) -> int:
    """
    Estimate conservative output token budget from source size.
    Avoids allocating an 8192-token budget for a 30-line DTO.
    """
    lines = src_content.count('\n') + 1
    # C# is ~20% more verbose than Java; Python/TS similar to source
    verbosity = 1.25 if tgt_lang == "csharp" else 1.1
    # ~15 tokens per source line is a reasonable estimate
    est = int(lines * 15 * verbosity)
    # Clamp: never below 1024 (handles boilerplate) or above _TOKENS_LARGE
    return max(1024, min(_TOKENS_LARGE, est + _CTX_SAFETY_MARGIN))


# ─── Path / namespace helpers ────────────────────────────────────────────────

# Function: _component_to_pascal
def _component_to_pascal(s: str) -> str:
    """Convert a Java package component or hyphenated folder name to PascalCase."""
    return "".join(w.capitalize() for w in re.split(r"[-_]+", s) if w)


_ORG_PREFIXES = {
    "com", "org", "net", "io", "edu", "gov", "co",
    "one", "de", "uk", "fr", "be", "nl", "cn", "jp",
}


# Function: _derive_root_namespace
def _derive_root_namespace(namespaces: list[str], folder_path: str = "") -> str:
    """
    Derive the C# root namespace from a list of Java package declarations.
    Finds the dominant prefix (merging short TLD-style first-level words with
    the next component) and returns it in PascalCase.
    e.g.  ["one.microproject.proxy", "one.microproject.files"] → "OneMicroproject"
          ["itx.examples.records"]                             → "Itx"
    """
    from collections import Counter
    prefix_counts: Counter[str] = Counter()
    for ns in namespaces:
        parts_ns = [p for p in ns.split(".") if p]
        if not parts_ns:
            continue
        if parts_ns[0].lower() in _ORG_PREFIXES and len(parts_ns) > 1:
            # Merge first two: "one" + "microproject" → "OneMicroproject"
            key = parts_ns[0].capitalize() + parts_ns[1].capitalize()
        else:
            key = parts_ns[0].capitalize()
        prefix_counts[key] += 1

    if not prefix_counts:
        name = Path(folder_path).name if folder_path else "App"
        return "".join(w.capitalize() for w in re.split(r"[-_\s]+", name) if w) or "App"

    return prefix_counts.most_common(1)[0][0]


# Function: _make_csharp_output_rel
def _make_csharp_output_rel(stripped: list[str], preserve_ext: bool, tgt_ext: str) -> str:
    # PascalCase each directory component — preserves Java package hierarchy
    # and avoids collisions when multiple sub-modules have same filename (e.g. Main.java)
    pascal_dirs = [_component_to_pascal(p) for p in stripped[:-1]]
    if preserve_ext:
        return "ModernizedApp/src/" + "/".join([*pascal_dirs, stripped[-1]])
    return "ModernizedApp/src/" + "/".join(
        [*pascal_dirs, Path(stripped[-1]).stem + tgt_ext]
    )


_JAVA_LEGACY_WEB_EXTS = {".css", ".htm", ".html", ".js", ".jsx", ".ts", ".tsx"}
_JAVA_RESOURCE_EXTS = {
    ".json", ".md", ".properties", ".sql", ".toml", ".txt", ".xml", ".yaml", ".yml",
}


# Function: _make_output_path
def _make_output_path(
    src_path: Path,
    folder_root: Path,
    target_lang: str,
    root_ns: str,
    target_stack: str,
) -> str:
    """
    Map a source file path to its output path inside ModernizedApp/.
    Preserves the directory structure mirrored under the appropriate service layout.
    """
    from .conversion_pipeline import _target_ext_for_lang
    try:
        rel = src_path.relative_to(folder_root)
    except ValueError:
        rel = Path(src_path.name)

    parts = list(rel.parts)
    src_ext = src_path.suffix.lower()
    tgt_ext = _target_ext_for_lang(target_lang)
    # IBM i projects commonly reuse the same member name across RPG, CL, PF,
    # LF and DSPF source files. A blind extension swap would overwrite all of
    # them as e.g. ORDER.java. Preserve the source role in the modern filename.
    ibmi_roles = {
        ".rpg": "RpgProgram", ".rpgle": "RpgProgram", ".sqlrpgle": "SqlRpgProgram",
        ".clp": "ClProgram", ".clle": "ClProgram", ".dds": "DdsSchema",
        ".pf": "PhysicalFile", ".lf": "LogicalFile", ".dspf": "DisplayFile",
        ".prtf": "PrinterFile", ".cpy": "Copybook",
    }
    legacy_roles = {
        ".cob": "CobolProgram", ".cbl": "CobolProgram",
        ".f": "FortranUnit", ".for": "FortranUnit", ".f90": "FortranUnit",
        ".f95": "FortranUnit", ".pas": "PascalUnit", ".pp": "PascalUnit",
        ".dpr": "DelphiProgram", ".pli": "PliProgram", ".pl1": "PliProgram",
        ".jcl": "JclJob", ".m": "MumpsRoutine", ".nsp": "NaturalProgram",
        ".nat": "NaturalProgram", ".p": "AblProgram", ".adb": "AdaBody",
        ".ads": "AdaSpec", ".ml": "OcamlModule", ".mli": "OcamlInterface",
        ".pro": "PrologRules", ".pl": "PrologRules",
    }
    source_roles = {**ibmi_roles, **legacy_roles}
    if src_ext in source_roles and parts:
        source_name = Path(parts[-1])
        parts[-1] = f"{source_name.stem}{source_roles[src_ext]}{src_ext}"
    # Config/resource files keep their original extension (not converted to .java/.cs etc.)
    _config_exts = _JAVA_RESOURCE_EXTS | {".html", ".htm", ".css"}
    preserve_ext = src_ext in _config_exts

    if target_lang == "java":
        stripped = _strip_source_root(parts)
        if src_ext in _JAVA_LEGACY_WEB_EXTS:
            # A Java modernization still has a browser surface. Treating a
            # legacy jQuery/JavaScript asset as backend source used to rename
            # it to *.java under src/main/java, after which Maven reported
            # meaningless compiler errors and the LLM repair fan-out appeared
            # to hang. Preserve the legacy evidence outside both compilers.
            out_rel = "ModernizedApp/frontend/public/legacy/" + "/".join(stripped)
        elif src_ext in _JAVA_RESOURCE_EXTS:
            # Keep runtime resources available without presenting them as Java
            # compilation input.
            out_rel = "ModernizedApp/src/main/resources/" + "/".join(stripped)
        else:
            out_rel = "ModernizedApp/src/main/java/" + "/".join(stripped)
            out_rel = _swap_ext(out_rel, tgt_ext)
    elif target_lang == "csharp":
        stripped = _strip_source_root(parts)
        out_rel = _make_csharp_output_rel(stripped, preserve_ext, tgt_ext)
    elif target_lang == "sql":
        out_rel = "ModernizedApp/Database/" + "/".join(parts)
    else:
        # Covers typescript/javascript/python and any other target — all use
        # the same flat "ModernizedApp/src/<mirrored path>" layout.
        stripped = _strip_source_root(parts)
        out_rel  = "ModernizedApp/src/" + "/".join(stripped)
        out_rel  = out_rel if preserve_ext else _swap_ext(out_rel, tgt_ext)

    return out_rel


# Function: _strip_source_root
def _strip_source_root(parts: list[str]) -> list[str]:
    """
    Remove common source root prefixes (src/main/java, src/, etc.).
    Searches the ENTIRE path so multi-module projects like
    `examples/entropy-demo/src/main/java/com/...` are stripped to `com/...`.
    """
    prefixes = [
        ["src", "main", "java"],
        ["src", "main", "kotlin"],
        ["src", "main", "groovy"],
        ["src", "main"],
        ["src", "test", "java"],
        ["src", "test", "kotlin"],
        ["src", "test"],
        ["src"],
        ["source"],
        ["sources"],
        ["main"],
        ["app"],
    ]
    lower = [p.lower() for p in parts]
    # Longest-match first: search anywhere in the path
    for prefix in prefixes:
        plen = len(prefix)
        for i in range(len(lower) - plen + 1):
            if lower[i:i + plen] == prefix:
                tail = parts[i + plen:]
                if tail:  # don't strip everything
                    return tail
    return parts


# Function: _swap_ext
def _swap_ext(path_str: str, new_ext: str) -> str:
    p = Path(path_str)
    return str(p.with_suffix(new_ext))
