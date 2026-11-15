# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — services/modernizer (validation_orchestration.py)
# Date: 2026-06-09
# ---------------------------------------------------------------------------
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING
from pathlib import Path
from collections.abc import Callable
import itertools

if TYPE_CHECKING:
    from services.validators import ValidationResult

logger = logging.getLogger(__name__)


_TS_NAMED_IMPORT_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)import\s+(?P<whole_type>type\s+)?"
    r"\{(?P<body>[^}]*)\}\s+from\s+(?P<module>['\"][^'\"]+['\"])\s*;?"
)
_TS_DEFAULT_TYPE_IMPORT_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)import\s+type\s+(?P<name>[A-Za-z_$][\w$]*)"
    r"\s+from\s+(?P<module>['\"][^'\"]+['\"])\s*;?"
)
_TS_NAMESPACE_TYPE_IMPORT_RE = re.compile(
    r"(?m)^(?P<indent>[ \t]*)import\s+type\s+\*\s+as\s+"
    r"(?P<name>[A-Za-z_$][\w$]*)\s+from\s+(?P<module>['\"][^'\"]+['\"])\s*;?"
)


def _typescript_symbol_has_runtime_use(content_without_imports: str, name: str) -> bool:
    """Return whether a type-only binding participates in JavaScript runtime code."""
    symbol = re.escape(name)
    return any(re.search(pattern, content_without_imports) for pattern in (
        rf"\bnew\s+{symbol}\b",
        rf"\binstanceof\s+{symbol}\b",
        rf"\b{symbol}\s*\.",
        rf"\b{symbol}\s*\(",
        rf"@\s*{symbol}\b",
        rf"\b(?:class\s+\w+\s+extends|extends)\s+{symbol}\b",
        rf"<\s*{symbol}(?:\s|/?>)",
    ))


def _repair_typescript_runtime_type_imports(content: str) -> str:
    """Promote only type-only bindings that are used as runtime values.

    Models frequently infer that exception classes or enums are "types" and
    emit ``import type`` even though the file constructs them or accesses a
    static member. TypeScript then reports TS1361. Split mixed imports rather
    than promoting every binding, preserving verbatim-module semantics for
    interfaces that genuinely remain type-only.
    """
    if not content or "import" not in content or "type" not in content:
        return content

    import_patterns = (
        _TS_NAMED_IMPORT_RE,
        _TS_DEFAULT_TYPE_IMPORT_RE,
        _TS_NAMESPACE_TYPE_IMPORT_RE,
    )
    usage = content
    for pattern in import_patterns:
        usage = pattern.sub(lambda match: "\n" * match.group(0).count("\n"), usage)

    def repair_named(match: re.Match) -> str:
        whole_type = bool(match.group("whole_type"))
        runtime_entries: list[str] = []
        regular_entries: list[str] = []
        type_entries: list[str] = []
        changed = False
        for raw_entry in match.group("body").split(","):
            entry = raw_entry.strip()
            if not entry:
                continue
            inline_type = entry.startswith("type ")
            clean = entry[5:].strip() if inline_type else entry
            local_name = re.split(r"\s+as\s+", clean, flags=re.IGNORECASE)[-1].strip()
            marked_type = whole_type or inline_type
            if marked_type and _typescript_symbol_has_runtime_use(usage, local_name):
                runtime_entries.append(clean)
                changed = True
            elif marked_type:
                type_entries.append(clean)
            else:
                regular_entries.append(clean)
        if not changed:
            return match.group(0)
        indent = match.group("indent")
        module = match.group("module")
        rows = []
        value_entries = [*regular_entries, *runtime_entries]
        if value_entries:
            rows.append(f"{indent}import {{ {', '.join(value_entries)} }} from {module};")
        if type_entries:
            rows.append(f"{indent}import type {{ {', '.join(type_entries)} }} from {module};")
        return "\n".join(rows)

    repaired = _TS_NAMED_IMPORT_RE.sub(repair_named, content)

    def repair_default(match: re.Match) -> str:
        name = match.group("name")
        if not _typescript_symbol_has_runtime_use(usage, name):
            return match.group(0)
        return f"{match.group('indent')}import {name} from {match.group('module')};"

    def repair_namespace(match: re.Match) -> str:
        name = match.group("name")
        if not _typescript_symbol_has_runtime_use(usage, name):
            return match.group(0)
        return f"{match.group('indent')}import * as {name} from {match.group('module')};"

    repaired = _TS_DEFAULT_TYPE_IMPORT_RE.sub(repair_default, repaired)
    return _TS_NAMESPACE_TYPE_IMPORT_RE.sub(repair_namespace, repaired)


_TS_FUNCTION_DECLARATION_ARROW_RE = re.compile(
    r"(?m)^(?P<head>[ \t]*(?:(?:export|default|declare|async)\s+)*"
    r"function\s+[A-Za-z_$][\w$]*(?:\s*<[^{}\r\n]+>)?\s*"
    r"\([^{}\r\n]*\)\s*(?::\s*[^={\r\n]+)?)\s*=>\s*\{"
)
_TS_CLASS_METHOD_ARROW_RE = re.compile(
    r"(?m)^(?P<head>[ \t]*(?:(?:public|private|protected|static|async|abstract|override)\s+)*"
    r"[A-Za-z_$][\w$]*(?:\s*<[^{}\r\n]+>)?\s*"
    r"\((?!\s*\()[^{}\r\n]*\)\s*(?::\s*[^={\r\n]+)?)\s*=>\s*\{"
)
_TS_CONST_ARROW_MISSING_RE = re.compile(
    r"(?m)^(?P<head>[ \t]*(?:export\s+)?const\s+[A-Za-z_$][\w$]*"
    r"(?:\s*:\s*[^\r\n]+?)?\s*=(?!>)\s*"
    r"(?:async\s*)?\([^{}\r\n]*\)\s*(?::\s*[^={\r\n]+)?)\s*\{"
)
_TS_INVALID_DEFAULT_CONST_RE = re.compile(
    r"\bexport\s+default\s+const\s+(?P<name>[A-Za-z_$][\w$]*)\b"
)


def _repair_typescript_declaration_arrows(content: str) -> str:
    """Repair TS1144 caused by mixing declarations with arrow syntax."""
    if not content or "=>" not in content:
        return content
    repaired = _TS_FUNCTION_DECLARATION_ARROW_RE.sub(
        lambda match: f"{match.group('head').rstrip()} {{", content,
    )
    return _TS_CLASS_METHOD_ARROW_RE.sub(
        lambda match: f"{match.group('head').rstrip()} {{", repaired,
    )


def _repair_typescript_const_arrows(content: str) -> str:
    """Repair a missing arrow in a const-assigned function or component."""
    if not content or "const" not in content:
        return content
    return _TS_CONST_ARROW_MISSING_RE.sub(
        lambda match: f"{match.group('head').rstrip()} => {{", content,
    )


def _repair_typescript_default_const(content: str) -> str:
    """Rewrite invalid ``export default const`` while preserving both exports."""
    names: list[str] = []

    def replace(match: re.Match) -> str:
        names.append(match.group("name"))
        return f"export const {match.group('name')}"

    repaired = _TS_INVALID_DEFAULT_CONST_RE.sub(replace, content or "")
    for name in dict.fromkeys(names):
        if not re.search(rf"\bexport\s+default\s+{re.escape(name)}\s*;", repaired):
            repaired = repaired.rstrip() + f"\nexport default {name};\n"
    return repaired


def _repair_typescript_unmatched_export_prefix(content: str, diagnostics) -> str:
    """Remove a compiler-proven unmatched closing token before an export line."""
    lines = content.splitlines(keepends=True)
    for diagnostic in diagnostics or ():
        match = re.search(r"\bline\s+(\d+):1:\s*TS1128\b", str(diagnostic))
        if not match:
            continue
        index = int(match.group(1)) - 1
        if not (0 <= index < len(lines)):
            continue
        line = lines[index]
        repaired_line = re.sub(
            r"^(?P<indent>[ \t]*)[};]+\s*(?=export\b)",
            lambda item: item.group("indent"), line, count=1,
        )
        if repaired_line != line:
            lines[index] = repaired_line
    return "".join(lines)


def _repair_typescript_missing_arrows_from_diagnostics(content: str, diagnostics) -> str:
    """Insert an arrow only at a body brace identified by the TS compiler."""
    lines = content.splitlines(keepends=True)
    for diagnostic in diagnostics or ():
        match = re.search(
            r"\bline\s+(\d+):(\d+):\s*TS1005:\s*['\"]?=>['\"]? expected\.",
            str(diagnostic),
        )
        if not match:
            continue
        index = int(match.group(1)) - 1
        column = int(match.group(2)) - 1
        if not (0 <= index < len(lines)):
            continue
        line = lines[index]
        if not line:
            continue
        brace_column = column if 0 <= column < len(line) and line[column] == "{" else None
        if brace_column is None:
            # TypeScript reports display columns while a model may indent with
            # tabs; Python indexes the tab as one character. Resolve only a
            # nearby brace and keep the compiler's `=> expected` diagnostic as
            # the required proof that this is a function body.
            nearby = [
                offset for offset, character in enumerate(line)
                if character == "{" and abs(offset - column) <= 8
            ]
            if not nearby:
                continue
            brace_column = min(nearby, key=lambda offset: abs(offset - column))
        # A variable type may legitimately contain its own arrow, as in
        # `const submit: () => void = async () {`. The compiler location
        # identifies the implementation body brace, so no syntax guessing is
        # needed and unrelated declarations remain untouched.
        lines[index] = (
            line[:brace_column].rstrip() + " => " + line[brace_column:]
        )
    return "".join(lines)


def _repair_typescript_malformed_default_export(
    content: str, diagnostics, rel_path: str,
) -> str:
    """Repair a compiler-proven malformed trailing export for the file component."""
    symbol = Path(rel_path).stem
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", symbol):
        return content
    lines = content.splitlines(keepends=True)
    diagnostic_lines = {
        int(match.group(1))
        for item in diagnostics or ()
        if (match := re.search(r"\bline\s+(\d+):\d+:\s*TS(?:1109|1128)\b", str(item)))
    }
    for line_number in sorted(diagnostic_lines):
        index = line_number - 1
        if not (0 <= index < len(lines)) or "export default" not in lines[index]:
            continue
        prior = "".join(lines[:index])
        declared_names = set(re.findall(
            r"\b(?:function|class|const|let)\s+([A-Za-z_$][\w$]*)\b", prior,
        ))
        export_tail = lines[index].split("export default", 1)[1]
        referenced_names = re.findall(r"\b[A-Za-z_$][\w$]*\b", export_tail)
        selected = symbol if symbol in declared_names else next(
            (name for name in reversed(referenced_names) if name in declared_names),
            "",
        )
        if not selected:
            continue
        already_default = re.search(
            rf"\bexport\s+default\s+(?:function\s+|class\s+)?{re.escape(selected)}\b",
            prior,
        )
        newline = "\r\n" if lines[index].endswith("\r\n") else "\n"
        lines[index] = newline if already_default else f"export default {selected};{newline}"
    return "".join(lines)


def _repair_typescript_source(content: str) -> str:
    """Apply safe, source-local TypeScript repairs before validation."""
    return _repair_typescript_default_const(
        _repair_typescript_const_arrows(
            _repair_typescript_declaration_arrows(
                _repair_typescript_runtime_type_imports(content)
            )
        )
    )


_CSHARP_NAMESPACE_RE = re.compile(
    r"(?m)^\s*namespace\s+([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*[;{]"
)
_CSHARP_TYPE_RE = re.compile(
    r"(?m)^\s*(?:(?:public|internal|protected|private|static|sealed|abstract|readonly|ref|new)\s+)*"
    r"(?:(partial)\s+)?(class|interface|record(?:\s+(?:class|struct))?|enum|struct)\s+"
    r"([A-Za-z_]\w*)"
)


def _csharp_type_declarations(content: str) -> list[tuple[str, str, str, bool]]:
    """Return namespace-qualified C# declarations suitable for cross-file checks.

    Generated files use one namespace per file, so a deliberately conservative
    parser is safer here than treating compiler diagnostics as an invitation to
    rewrite a random duplicate. Nested/helper declarations are retained in the
    result and therefore prevent destructive reconciliation of mixed-purpose files.
    """
    namespace_match = _CSHARP_NAMESPACE_RE.search(content or "")
    namespace = namespace_match.group(1) if namespace_match else ""
    declarations: list[tuple[str, str, str, bool]] = []
    for match in _CSHARP_TYPE_RE.finditer(content or ""):
        partial, kind, name = match.groups()
        declarations.append((namespace, kind.split()[0], name, bool(partial)))
    return declarations


def _csharp_canonical_path_score(path: str, namespace: str, type_name: str) -> tuple[int, int, str]:
    """Prefer conventional, namespace-aligned paths for a duplicated C# type."""
    normalized = path.replace("\\", "/")
    parts = [part.casefold() for part in normalized.split("/") if part]
    parent = parts[:-1]
    namespace_parts = [part.casefold() for part in namespace.split(".") if part]
    score = 0
    if parts and parts[-1] == f"{type_name.casefold()}.cs":
        score += 100
    if namespace_parts and parent and parent[-1] == namespace_parts[-1]:
        score += 50
    tail_size = min(len(namespace_parts), len(parent))
    if tail_size and parent[-tail_size:] == namespace_parts[-tail_size:]:
        score += 25
    score -= sum(40 for left, right in itertools.pairwise(parent) if left == right)
    return score, -len(parts), normalized.casefold()


def _reconcile_csharp_duplicate_types(output: dict[str, str]) -> dict[str, str]:
    """Suppress safe-to-remove duplicate C# files before whole-project builds.

    Only files made up entirely of duplicated, non-partial declarations are
    changed. The manifest path remains as a comment-only trace file, avoiding a
    false "missing required file" audit while allowing MSBuild to compile the
    single canonical implementation. Likewise, a nested ``Program.cs`` without
    its own project manifest is suppressed when its owning project already has
    a canonical root entrypoint.
    """
    declarations_by_path: dict[str, list[tuple[str, str, str, bool]]] = {}
    owners: dict[tuple[str, str], list[str]] = {}
    for path, content in output.items():
        if not path.casefold().endswith(".cs") or not isinstance(content, str):
            continue
        declarations = _csharp_type_declarations(content)
        if not declarations:
            continue
        declarations_by_path[path] = declarations
        for namespace, _kind, name, partial in declarations:
            if not partial:
                owners.setdefault((namespace, name), []).append(path)

    canonical: dict[tuple[str, str], str] = {}
    for key, paths in owners.items():
        distinct_paths = list(dict.fromkeys(paths))
        if len(distinct_paths) > 1:
            namespace, name = key
            canonical[key] = max(
                distinct_paths,
                key=lambda path: _csharp_canonical_path_score(path, namespace, name),
            )

    reconciled: dict[str, str] = {}
    for path, declarations in declarations_by_path.items():
        keys = [(namespace, name) for namespace, _kind, name, partial in declarations if not partial]
        if not keys or len(keys) != len(declarations):
            continue
        winners = {canonical.get(key) for key in keys}
        if None in winners or path in winners:
            continue
        winner_paths = sorted(winner for winner in winners if winner)
        output[path] = (
            "// Redundant generated type definition suppressed before build.\n"
            f"// Canonical implementation: {', '.join(winner_paths)}\n"
        )
        reconciled[path] = ", ".join(winner_paths)

    normalized_paths = {path.replace("\\", "/"): path for path in output}
    project_dirs = sorted({
        path.rsplit("/", 1)[0]
        for path in normalized_paths
        if path.casefold().endswith(".csproj") and "/" in path
    }, key=len, reverse=True)
    for normalized, original_path in list(normalized_paths.items()):
        if not normalized.casefold().endswith("/program.cs"):
            continue
        owner = next(
            (directory for directory in project_dirs if normalized.startswith(directory + "/")),
            None,
        )
        if not owner:
            continue
        canonical_program = f"{owner}/Program.cs"
        canonical_original = normalized_paths.get(canonical_program)
        if not canonical_original or original_path == canonical_original:
            continue
        output[original_path] = (
            "// Redundant generated project entrypoint suppressed before build.\n"
            f"// Canonical implementation: {canonical_original}\n"
        )
        reconciled[original_path] = canonical_original
    return reconciled



# Function: _requirements_assessment
def _requirements_assessment(user_prompt: str, manifest: list[str]) -> str:
    """Keep contracts and acceptance criteria prominent in every file call."""
    sections = []
    for heading in ("OBJECTIVE", "CANONICAL CONTRACTS", "HARD ACCEPTANCE CRITERIA",
                    "DEFECTS TO EXPLICITLY AVOID"):
        # Two-step: find the heading and how many '#' it uses, THEN stop only
        # at another heading of the same or shallower depth. A single-step
        # "stop at any #{1,3} heading" lookahead stops at the section's own
        # "### Entities" / "### DTOs" / ... subheadings (CANONICAL CONTRACTS
        # is entirely organized as ### subsections), capturing nothing for
        # exactly the section the whole exercise revolves around.
        start = re.search(rf"(?im)^\s*(#{{1,3}})\s*{re.escape(heading)}\b[^\n]*\n", user_prompt)
        if not start:
            continue
        level = len(start.group(1))
        rest = user_prompt[start.end():]
        stop = re.search(rf"(?im)^\s*#{{1,{level}}}\s", rest)
        body = (rest[:stop.start()] if stop else rest).strip()
        if body:
            sections.append(f"{heading}:\n{body}")
    if manifest:
        sections.append("AUTHORITATIVE OUTPUT MANIFEST:\n" +
                        "\n".join(f"- {path}" for path in manifest))
    return ("\n\nDETAILED REQUIREMENTS ASSESSMENT (authoritative; self-check against it "
            "before returning the file):\n" + "\n\n".join(sections)) if sections else ""


# Function: _generation_template
def _generation_template(
    user_prompt: str,
    target: dict,
    signals: dict[str, str | None],
    manifest: list[str],
) -> str:
    """General template model supplied to planning and every generation call."""
    layers = [
        f"frontend={target.get('frontend_tech')}",
        f"backend={target.get('backend_tech')}",
        f"data={target.get('db_tech')}",
    ]
    layers.extend(
        f"{key}={signals[key]}"
        for key in ("auth", "orm", "deploy", "db")
        if signals.get(key)
    )
    manifest_policy = (
        "The supplied manifest is authoritative: emit every listed file exactly once."
        if manifest else
        "Derive the smallest complete manifest, including entry points, configuration, "
        "dependency manifests, feature code, tests, and deployment assets."
    )
    return (
        "\n\nGENERATION TEMPLATE MODEL (mandatory for every technology stack):\n"
        "1. Requirements: preserve explicit versions, constraints, endpoints, and acceptance criteria.\n"
        "2. Architecture: use separate presentation, application/service, domain/model, data-access, "
        "configuration, test, and deployment concerns where the selected stack supports them.\n"
        "3. Contracts first: define each public type once; implementations and tests may call only "
        "members present on the corresponding interface/type with identical names and arity.\n"
        "4. Bootstrap completeness: include every framework entry point/root module and register every "
        "dependency, authentication scheme, route, middleware, health endpoint, and configuration source.\n"
        "5. Cross-file closure: every local import/reference and every configured asset/path must exist "
        "in the output; no duplicate types, placeholders, TODOs, omitted bodies, or markdown fences.\n"
        "6. Operational consistency: Docker contexts, ports, probes, service names, configuration keys, "
        "and deployment selectors must agree end-to-end.\n"
        "7. Verification: self-check compilation-facing contracts, imports, manifests, and prohibited "
        "patterns before returning each file.\n"
        f"Resolved stack: {'; '.join(layers)}.\n"
        f"Manifest policy: {manifest_policy}"
    )


# Function: _audit_generated_project
def _audit_generated_project(
    output: dict[str, str],
    project_name: str,
    expected_files: list[str],
) -> list[str]:
    """Cross-file structural checks that need the whole project at once (missing
    manifest files, duplicate C# type declarations across files). Per-file checks
    (empty file, markdown fence, placeholder/TODO text) are covered earlier, per
    file, by services/validators.py's validate_file — every LLM-generated file
    already went through that before landing in `output`."""
    expected_keys = {f"{project_name}/{path}" for path in expected_files}
    issues = [
        f"missing required file: {missing}"
        for missing in sorted(expected_keys - set(output))
    ]

    type_owners: dict[tuple[str, str], str] = {}
    for path, content in output.items():
        if not isinstance(content, str) or not content.strip() or not path.lower().endswith(".cs"):
            continue
        for namespace, kind, name, partial in _csharp_type_declarations(content):
            if partial:
                continue
            key = (namespace, name)
            owner = type_owners.get(key)
            if owner and owner != path:
                qualified_name = f"{namespace}.{name}" if namespace else name
                issues.append(f"duplicate {kind} {qualified_name}: {owner} and {path}")
            else:
                type_owners[key] = path
    return issues


# Function: _clean_generated_content
def _clean_generated_content(content: str) -> str:
    """Remove response-format artifacts that make valid source uncompilable.

    A perfectly-formed single fenced block (```lang\\n...\\n```) with nothing
    else around it was the only case the old `re.fullmatch` check handled —
    every prompt says "no markdown fences", but a local model still routinely
    adds a lead-in sentence ("Here is the complete file:") or a trailing
    explanation around the fence anyway. Any deviation from that one exact
    shape left the fence markers completely unstripped, so the compiler saw a
    literal ``` as its first token — "illegal character: '`'" — on every one
    of the repair loop's retries, since the diagnostics fed back to the model
    describe a *compile* error, not "you still included markdown fences", so
    the model has no signal to fix the actual problem. This instead scans
    line-by-line for fence-marker lines (a line that is only ``` or
    ```<lang>, which is never legitimate source in any language this
    pipeline generates) and keeps only what is between the first pair,
    dropping any prose outside them — recovering the file regardless of what
    the model wrapped around it.
    """
    text = (content or "").strip()
    if not text:
        return text
    # Governed generation normally parses this envelope before reaching the
    # cleaner.  Repair and cached-domain routes can still receive the same
    # response with optional attributes (``path=``/``filename=``), or a
    # token-limited response containing only the opening tag.  Leaving that
    # tag in a TSX file turns the real truncation into a misleading JSX error
    # cascade and can keep the file outside the correct repair lane.  Remove
    # only a leading response envelope; an absent closing tag deliberately
    # leaves the incomplete source in place so strict validation still fails
    # closed and requests a repair.
    envelope = re.match(
        r"(?is)^<generated_file(?:\s+[^>]*)?>\s*", text,
    )
    if envelope:
        text = text[envelope.end():]
        closing = re.search(r"(?is)\s*</generated_file>\s*", text)
        if closing:
            text = text[:closing.start()]
    # Some local-model repair responses append governance protocol fragments
    # even when the prompt did not request an envelope. They are transport
    # metadata, never valid source. Truncate a complete notes section and
    # remove orphan closing tags only when they occupy their own line, so
    # string literals containing similar text remain untouched.
    notes_open = re.search(
        r"(?im)^[ \t]*<generation_notes(?:\s+[^>]*)?>[ \t]*$", text,
    )
    if notes_open:
        text = text[:notes_open.start()].rstrip()
    text = re.sub(
        r"(?im)^[ \t]*</?(?:generated_file|generation_notes)>[ \t]*(?:\r?\n|$)",
        "",
        text,
    )
    lines = text.splitlines()
    fence_positions = [i for i, line in enumerate(lines) if re.fullmatch(r"```[\w.+-]*", line.strip())]
    if fence_positions:
        start = fence_positions[0] + 1
        end = fence_positions[1] if len(fence_positions) > 1 else len(lines)
        lines = lines[start:end]
    text = "\n".join(lines).strip()
    # Recover a complete Java/Kotlin/Scala unit when a continuation restarted
    # at `package` and was concatenated after an incomplete first draft. A
    # source file cannot legally contain a second package declaration.
    package_declarations = list(re.finditer(
        r"(?m)(?:^|(?<=[;}]))[ \t]*package\s+[A-Za-z_$][\w.$]*\s*;",
        text,
    ))
    if len(package_declarations) > 1:
        text = text[package_declarations[-1].start():].lstrip()
    return text + ("\n" if text else "")


def _validation_diagnostic_fingerprint(diagnostics) -> tuple[str, ...]:
    """Normalize location-only churn so an ineffective repair stops early."""
    normalized = []
    for diagnostic in diagnostics or ():
        value = str(diagnostic).casefold()
        value = re.sub(r"\bline\s+\d+(?::\d+)?\b", "line", value)
        value = re.sub(r"\[\d+[, :]\d+\]", "[location]", value)
        value = re.sub(r"\(\d+[, :]\d+\)", "(location)", value)
        normalized.append(" ".join(value.split()))
    return tuple(sorted(normalized))


def _validation_failure_excerpt(content: str, diagnostics, *, radius: int = 1) -> str:
    """Return bounded generated-source context around compiler-reported lines."""
    source_lines = (content or "").splitlines()
    reported = {
        int(match.group(1))
        for item in diagnostics or ()
        if (match := re.search(r"\bline\s+(\d+)(?::\d+)?\b", str(item)))
    }
    selected: set[int] = set()
    for line_number in reported:
        selected.update(range(max(1, line_number - radius), line_number + radius + 1))
    if not selected:
        nonempty = [
            index for index, line in enumerate(source_lines, start=1) if line.strip()
        ]
        selected.update(nonempty[:6])
        selected.update(nonempty[-2:])
    snippets = []
    for line_number in sorted(selected):
        if 1 <= line_number <= len(source_lines):
            value = source_lines[line_number - 1].replace("\t", "\\t").rstrip()[:240]
            snippets.append(f"{line_number}: {value}")
    return " | ".join(snippets[:12])


def _truncation_shaped_diagnostics(diagnostics) -> bool:
    """Identify parser/compiler evidence that a generated file ended early."""
    markers = (
        "reached end of file", "unexpected end", "unexpected eof", "unterminated",
        "unclosed", "missing outer end", "is incomplete:",
        "has no corresponding closing tag",
        "expected corresponding jsx closing tag", "expected '</'", "'</' expected",
        "'}' expected", "')' expected", "']' expected", "end of input",
    )
    return any(
        marker in str(diagnostic).casefold()
        for diagnostic in diagnostics or ()
        for marker in markers
    )


# Jakarta EE 9 renamed a fixed, well-known set of javax.* packages to
# jakarta.* with no semantic change — an unambiguous, mechanical rename, not
# a judgment call. Scoped to exactly the packages Spring Boot 3 code this
# pipeline generates actually uses (validation, persistence, servlet, JAX-RS,
# common annotations, transactions) — deliberately NOT a blanket javax.*
# prefix swap, since plenty of javax.* packages (javax.sql, javax.crypto,
# javax.xml.parsers, javax.swing, …) are core JDK APIs that never moved and
# must not be touched.
_JAVAX_TO_JAKARTA_IMPORT = re.compile(
    r"^(\s*import\s+(?:static\s+)?)javax\.(validation|persistence|servlet|ws\.rs|annotation|transaction)\.",
    re.MULTILINE,
)


def _normalize_jakarta_namespace(content: str) -> str:
    """Rewrite javax.* imports Jakarta EE 9 renamed to jakarta.* for Spring
    Boot 3 targets. A small local model reliably reverts to the javax.*
    spelling it saw far more often in training regardless of how many times
    a repair prompt says otherwise — observed directly: a Controller failing
    all 3 repair attempts on nothing but `import javax.validation.Valid`,
    having already fixed every other reported compiler error along the way.
    Since the rename is unambiguous and mechanical, apply it deterministically
    instead of spending repair-round budget hoping the model complies — there
    is no legitimate reason to keep the javax.* spelling here for a Spring
    Boot 3 target, so this can only ever help."""
    return _JAVAX_TO_JAKARTA_IMPORT.sub(r"\1jakarta.\2.", content)


# Function: _single_file_extension
def _single_file_extension(language: str, content: str) -> str:
    """Choose the compiler-facing extension for standalone generated source.

    TypeScript decides whether JSX grammar is legal from the filename. Passing a
    React component to tsc as ``generated.ts`` produces a misleading cascade of
    TS1xxx parse errors (often beginning with TS1161), even when the source is
    otherwise valid TSX.
    """
    from ._shared import _DEFAULT_EXT_FOR_LANG
    language = (language or "").lower()
    if language not in {"typescript", "javascript"}:
        return _DEFAULT_EXT_FOR_LANG.get(language, ".txt")

    from services.validators import _contains_jsx
    has_jsx = _contains_jsx(content)
    if language == "typescript":
        return ".tsx" if has_jsx else ".ts"
    return ".jsx" if has_jsx else ".js"


def _normalize_oracle_package_terminators(content: str, dialect: str) -> str:
    """Normalize SQL*Plus separators at executable PL/SQL unit boundaries.

    A slash on a line by itself is a SQL*Plus client command which executes the
    preceding buffer; it is not an Oracle SQL statement terminator. Models
    recurrently append it after INSERT/DDL as if it were a generic delimiter.
    Remove those unsafe separators first, then add a missing separator after a
    complete package unit. Completed anonymous blocks, procedures, functions,
    triggers, and package/type bodies already ending in ``END ...;`` retain
    their separator. Keeping this operation deterministic and idempotent means
    compiler-repair output cannot reintroduce the same failure indefinitely.
    """
    if not any(token in (dialect or "").casefold() for token in ("oracle", "pl/sql")):
        return content
    from services.validators import _mask_sql_literals_and_comments

    source = content or ""
    masked_lines = _mask_sql_literals_and_comments(source).splitlines(keepends=True)
    source_lines = source.splitlines(keepends=True)
    # Masking preserves line boundaries. Leave unfamiliar input untouched if
    # that invariant is ever changed by a future tokenizer implementation.
    if len(masked_lines) != len(source_lines):
        return content
    keep = [True] * len(source_lines)
    for index, line in enumerate(masked_lines):
        if line.strip() != "/":
            continue
        previous = next(
            (masked_lines[position].strip() for position in range(index - 1, -1, -1)
             if masked_lines[position].strip()),
            "",
        )
        if not re.search(
            r"(?i)^END(?:\s+[A-Za-z][A-Za-z0-9_$#]*)?\s*;\s*$", previous,
        ):
            keep[index] = False
    content = "".join(
        line for index, line in enumerate(source_lines) if keep[index]
    )
    declaration = re.compile(
        r"(?im)^\s*CREATE\s+OR\s+REPLACE\s+PACKAGE\s+(?:BODY\s+)?"
        r"(\"?[A-Za-z][A-Za-z0-9_$#]*\"?)\b[^\n]*?\b(?:AS|IS)\b"
    )
    units = list(declaration.finditer(content or ""))
    insertions: list[int] = []
    for unit_index, match in enumerate(units):
        unit_end = units[unit_index + 1].start() if unit_index + 1 < len(units) else len(content)
        segment = content[match.end():unit_end]
        terminators = list(re.finditer(
            r"(?im)^\s*END(?:\s+(\"?[A-Za-z][A-Za-z0-9_$#]*\"?))?\s*;\s*$",
            segment,
        ))
        if not terminators:
            continue
        outer_end = terminators[-1]
        absolute_end = match.end() + outer_end.end()
        trailing = content[absolute_end:unit_end]
        if not re.match(r"\s*/(?:\s|$)", trailing):
            insertions.append(absolute_end)
    normalized = content
    for position in reversed(insertions):
        normalized = normalized[:position].rstrip() + "\n/\n" + normalized[position:].lstrip("\r\n")
    return normalized


def _repair_oracle_bind_variable_views(content: str, dialect: str) -> str:
    """Convert impossible parameterized Oracle views into runnable queries.

    Oracle views cannot declare or consume bind variables. Generated date-range
    and account-filter deliverables nevertheless recurrently wrapped a valid
    parameterized SELECT in ``CREATE VIEW ... AS``. Removing only that wrapper
    preserves the query, bind contract, aliases, ordering, and result shape and
    yields an executable SQL*Plus/driver query. A function remains appropriate
    when the requirement explicitly asks for a stored database object, but it
    cannot be synthesized safely from an arbitrary SELECT mechanically.
    """
    if not any(token in (dialect or "").casefold() for token in ("oracle", "pl/sql")):
        return content
    from services.validators import _mask_sql_literals_and_comments

    source = content or ""
    masked = _mask_sql_literals_and_comments(source)
    declaration = re.compile(
        r"(?is)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FORCE\s+)?VIEW\s+"
        r"(?P<name>[A-Za-z][A-Za-z0-9_$#]*)\b"
        r"(?:\s*\([^;]*?\))?\s+AS\s+(?P<body>.*?);",
    )
    replacements: list[tuple[int, int, str]] = []
    for match in declaration.finditer(masked):
        body = match.group("body")
        if not re.search(r"(?<!:):[A-Za-z][A-Za-z0-9_$#]*\b", body):
            continue
        replacements.append((
            match.start(), match.start("body"),
            f"-- Parameterized Oracle query {match.group('name')} "
            "(views cannot contain bind variables)\n",
        ))
    repaired = source
    for start, end, replacement in reversed(replacements):
        repaired = repaired[:start] + replacement + repaired[end:]
    return repaired


def _repair_oracle_multirow_values(content: str, dialect: str) -> str:
    """Convert non-Oracle multi-row VALUES inserts to Oracle INSERT ALL.

    Oracle targets before 23c do not accept PostgreSQL/MySQL-style
    ``INSERT ... VALUES (...), (...)``. The table, column list, and every value
    tuple make this a lossless structural rewrite; anything more complex is
    deliberately left to the compiler rather than guessed.
    """
    if not any(token in (dialect or "").casefold() for token in ("oracle", "pl/sql")):
        return content
    from services.validators import _mask_sql_literals_and_comments

    source = content or ""
    masked = _mask_sql_literals_and_comments(source)
    statement = re.compile(
        r"(?is)\bINSERT\s+INTO\s+"
        r"(?P<table>(?:\"[^\"]+\"|[A-Za-z][A-Za-z0-9_$#]*)"
        r"(?:\s*\.\s*(?:\"[^\"]+\"|[A-Za-z][A-Za-z0-9_$#]*))?)\s*"
        r"(?P<columns>\([^;]*?\))\s+VALUES\s+(?P<rows>.*?);",
    )
    replacements: list[tuple[int, int, str]] = []
    for match in statement.finditer(masked):
        rows_masked = match.group("rows")
        tuples: list[tuple[int, int]] = []
        depth = 0
        start: int | None = None
        for index, character in enumerate(rows_masked):
            if character == "(":
                if depth == 0:
                    start = index
                depth += 1
            elif character == ")" and depth:
                depth -= 1
                if depth == 0 and start is not None:
                    tuples.append((start, index + 1))
                    start = None
        if depth or len(tuples) < 2:
            continue
        # Only commas, whitespace, and masked comments may occur around/between
        # tuples. RETURNING, SELECT, or other clauses are intentionally skipped.
        residual = list(rows_masked)
        for start, end in tuples:
            residual[start:end] = " " * (end - start)
        if re.sub(r"[\s,]", "", "".join(residual)):
            continue
        rows_source = source[match.start("rows"):match.end("rows")]
        table = source[match.start("table"):match.end("table")].strip()
        columns = source[match.start("columns"):match.end("columns")].strip()
        values = [rows_source[start:end].strip() for start, end in tuples]
        replacement = "INSERT ALL\n" + "\n".join(
            f"  INTO {table} {columns} VALUES {value}" for value in values
        ) + "\nSELECT 1 FROM DUAL;"
        replacements.append((match.start(), match.end(), replacement))
    repaired = source
    for start, end, replacement in reversed(replacements):
        repaired = repaired[:start] + replacement + repaired[end:]
    return repaired


# Function: _generate_validated
def _generate_validated(
    prompt: str,
    *,
    model: str,
    system: str,
    max_tokens: int,
    num_ctx: int,
    rel_path: str,
    language: str,
    dialect: str = "",
    on_token: Callable[[str], None] | None = None,
    on_repair_token: Callable[[str], None] | None = None,
    on_attempt: Callable[[int, int], None] | None = None,
    max_attempts: int = 3,
    detect_language: bool = False,
    think_initial: bool | None = None,
    generation_max_seconds: float | None = None,
    generation_attempt_max_seconds: float | None = None,
    require_governance_envelope: bool = False,
    initial_content: str | None = None,
    forbid_jsx: bool = False,
) -> tuple[str, ValidationResult, int]:
    """
    Generate one file's content, then syntax-validate it (see services/validators.py)
    and retry with the diagnostics fed back to the model on failure, up to
    max_attempts total. Never raises on exhausted retries — returns the last
    attempt with result.passed=False so callers can report it without failing
    the whole job (same "report, don't discard" philosophy as _audit_generated_project).
    generate() exceptions (e.g. Ollama unreachable) propagate unchanged — this
    only changes what happens to successfully-returned-but-bad content.
    ``initial_content`` starts at validation/repair without making another
    initial generation call; structural generation uses this to repair its
    already-produced complete artifact instead of discarding it.
    `dialect` is only used for language="sql" (pass the target's db_tech string
    so sqlglot parses against the right SQL dialect).
    """
    from ._shared import _TOKENS_XLARGE, _adaptive_num_ctx
    from services.llm import generate, pick_compiler_repair_model
    from services.generation_governance import (
        _first_placeholder, generation_notes_snapshot, parse_generation_response,
    )
    from services.validators import _infer_sql_dialect, _resolve_sql_dialect, validate_file

    def deterministic_source_repair(current: str, current_result):
        if current_result.passed:
            return current, current_result
        if current_result.language in {"typescript", "javascript"}:
            repaired = current
            repaired_result = current_result
            # A malformed response can contain independent parser defects.
            # Revalidate after each deterministic pass so fixing an early
            # missing arrow can expose and then repair a trailing export error
            # without spending another model inference attempt.
            for _ in range(3):
                candidate = _repair_typescript_missing_arrows_from_diagnostics(
                    repaired, repaired_result.diagnostics,
                )
                candidate = _repair_typescript_malformed_default_export(
                    candidate, repaired_result.diagnostics, validation_path,
                )
                candidate = _repair_typescript_unmatched_export_prefix(
                    candidate, repaired_result.diagnostics,
                )
                candidate = _repair_typescript_source(candidate)
                if candidate == repaired:
                    break
                repaired = candidate
                repaired_result = validate_file(
                    validation_path, repaired, current_result.language,
                    dialect_hint=dialect,
                )
                if repaired_result.passed:
                    break
            return repaired, repaired_result
        if current_result.language != "java":
            return current, current_result
        from .build_artifacts import _apply_deterministic_java_diagnostic_repairs
        candidate = {validation_path: current}
        changed = _apply_deterministic_java_diagnostic_repairs(
            candidate, {validation_path: list(current_result.diagnostics)},
        )
        if not changed:
            return current, current_result
        repaired = candidate[validation_path]
        return repaired, validate_file(
            validation_path, repaired, "java", dialect_hint=dialect,
        )

    def completeness_gate(current: str, current_result):
        """Route syntax-valid stubs through the normal bounded repair loop."""
        if not current_result.passed:
            return current_result
        placeholder = _first_placeholder(current, validation_path)
        if not placeholder:
            return current_result
        line = current.count("\n", 0, placeholder.start()) + 1
        return type(current_result)(
            current_result.path, current_result.language, "source-completeness", False,
            [
                f"line {line}: replace incomplete placeholder construct "
                f"{placeholder.group(0)!r} with a complete implementation"
            ],
        )

    generation_started = time.monotonic()
    generation_deadline = (
        generation_started + generation_max_seconds
        if generation_max_seconds else None
    )

    def generation_budget() -> float | None:
        """Return this attempt's budget without exceeding the aggregate deadline."""
        if language != "java":
            # Non-Java callers historically supply a per-attempt timeout.
            # Preserve that exact contract instead of shaving monotonic-clock
            # setup time from every retry. Java generation deliberately uses
            # one aggregate budget because its draft and repair calls are much
            # larger and must share a single bounded window.
            budgets = [
                value for value in (generation_max_seconds, generation_attempt_max_seconds)
                if value is not None
            ]
            return min(budgets) if budgets else None
        if generation_deadline is None:
            return generation_attempt_max_seconds
        remaining = generation_deadline - time.monotonic()
        if remaining <= 0:
            label = "Java" if language == "java" else language.title()
            raise TimeoutError(
                f"{label} generation exceeded the {generation_max_seconds:.0f}s "
                "aggregate per-file budget"
            )
        if generation_attempt_max_seconds:
            return min(remaining, generation_attempt_max_seconds)
        return remaining

    inference_started = time.monotonic()
    if initial_content is None:
        raw_response = generate(
            prompt, model=model, system=system, max_tokens=max_tokens, num_ctx=num_ctx,
            on_token=on_token, think=think_initial, max_seconds=generation_budget(),
        )
        content = _clean_generated_content(
            parse_generation_response(raw_response, rel_path)
            if require_governance_envelope or raw_response.lstrip().startswith("<generated_file>")
            else raw_response
        )
    else:
        content = _clean_generated_content(initial_content)
    if language in {"typescript", "javascript"} or Path(rel_path).suffix.casefold() in {".ts", ".tsx", ".js", ".jsx"}:
        content = _repair_typescript_source(content)
    if language == "sql":
        content = _normalize_oracle_package_terminators(content, dialect)
        content = _repair_oracle_multirow_values(content, dialect)
        content = _repair_oracle_bind_variable_views(content, dialect)
    if language == "java":
        content = _normalize_jakarta_namespace(content)
        logger.info(
            "Java generation timing path=%s attempt=1 inference=%.3fs prompt_chars=%d output_chars=%d",
            rel_path, time.monotonic() - inference_started, len(prompt), len(content),
        )
    validation_language = _detect_single_file_language(content, language) if detect_language else language
    validation_path = (
        f"generated{_single_file_extension(validation_language, content)}"
        if detect_language else rel_path
    )
    validation_started = time.monotonic()
    result = validate_file(validation_path, content, validation_language, dialect_hint=dialect)
    content, result = deterministic_source_repair(content, result)
    result = completeness_gate(content, result)
    response_notes = generation_notes_snapshot().get(rel_path, {})
    if require_governance_envelope and not response_notes.get("format_valid"):
        result = type(result)(
            result.path, result.language, "generation-envelope", False,
            [*result.diagnostics, str(response_notes.get("error") or "missing governed generation notes")],
        )
    if language == "java":
        logger.info(
            "Java generation timing path=%s attempt=1 validation=%.3fs passed=%s diagnostics=%d",
            rel_path, time.monotonic() - validation_started, result.passed, len(result.diagnostics),
        )
    attempt = 1
    repair_model = pick_compiler_repair_model(model) if language == "cobol" else model
    cobol_fixed = language == "cobol" and any(
        token in (dialect or "").lower() for token in ("ibm", "db2", "z/os", "zos", "enterprise cobol")
    )

    while not result.passed and attempt < max_attempts:
        attempt += 1
        if on_attempt:
            on_attempt(attempt, max_attempts)
        _pre_repair_diagnostics = _validation_diagnostic_fingerprint(result.diagnostics)
        truncation_recovery = _truncation_shaped_diagnostics(result.diagnostics)
        repair_max_tokens = (
            max(max_tokens, _TOKENS_XLARGE)
            if truncation_recovery else max_tokens
        )
        diagnostics_block = "\n".join(f"- {d}" for d in result.diagnostics) or "- (no specific diagnostics)"
        java_file_identity_mismatch = bool(
            validation_language == "java"
            and any(
                marker in str(item)
                for item in result.diagnostics
                for marker in (
                    "must declare its filename-matching type",
                    "contains no type declaration",
                    "must declare path-matching package",
                    "does not match source path package",
                )
            )
        )
        language_repair_rules = ""
        if validation_language in {"typescript", "javascript"}:
            language_repair_rules = (
                "\nTypeScript/JavaScript compiler rules: Preserve JSX only when the requested "
                "file is a React/JSX component; standalone TypeScript logic must not contain JSX. "
                "Balance every JSX opening/closing tag, brace, parenthesis, bracket, string, "
                "template literal, and regular-expression delimiter. Do not write HTML markup "
                "outside a JSX expression. Never combine declaration and arrow syntax: use "
                "`function load() {}` or `const load = () => {}`, and use `load() {}` for a "
                "class method; never emit `function load() => {}` or `load() => {}` as a method. "
                "Return one complete source file, never a markdown "
                "fence or a filename header.\n"
            )
            if forbid_jsx:
                language_repair_rules += (
                    "This file is a non-React plain TypeScript module. Its .ts path is "
                    "authoritative and cannot contain JSX grammar. Do not rename it to .tsx. "
                    "Remove React imports, hooks, components, HTML tags, fragments, and UI "
                    "rendering; implement only the requested data/transport/runtime contract.\n"
                )
        if validation_language == "java":
            language_repair_rules = (
                "\nJava/Spring Boot 3 repair rules: use jakarta.* rather than legacy javax.* "
                "enterprise APIs. Controllers declare request inputs directly on method "
                "parameters. If Idempotency-Key is required, use exactly "
                '`@RequestHeader(name = "Idempotency-Key") String idempotencyKey` in the '
                "mapped controller method and pass that value to the service; do not use "
                "HttpServletRequest, RequestContextHolder, ServletRequestAttributes, or a "
                "local header-name string as a substitute. Use constructor injection and "
                "typed exceptions handled by @RestControllerAdvice. A controller depends on "
                "an application service only: it must not access repositories or Kafka/event "
                "publishers directly. DTOs, records, enums, and status types belong in their "
                "own manifest files, never as private nested controller classes. Add every "
                "required java.util/java.time/java.math import explicitly.\n"
            )
        if validation_language == "sql":
            effective_sql_dialect = (
                _resolve_sql_dialect(dialect) or _infer_sql_dialect(content) or "ANSI"
            )
            oracle_package_rules = (
                " For Oracle PL/SQL packages, keep exactly one complete package specification "
                "and one complete package body when public routines are declared. End each unit "
                "with its matching package name. A package "
                "body may contain declarations and PL/SQL subprograms, but CREATE, ALTER, DROP, "
                "and TRUNCATE DDL inside a routine must be passed as a string to EXECUTE IMMEDIATE; "
                "never place DDL directly inside BEGIN/END. Balance every routine, IF, LOOP, CASE, "
                "BEGIN, and END. Rewrite the complete package body when the parser identifies the "
                "whole body as unparsable. Views cannot contain bind variables: convert a "
                "bind-dependent view to a standalone parameterized SELECT or implement a "
                "typed function/procedure when a stored object is explicitly required. A "
                "SQL*Plus slash on its own line is permitted only after a complete PL/SQL "
                "block or stored unit ending in END [name]; never after SELECT, INSERT, "
                "UPDATE, DELETE, MERGE, CREATE TABLE/INDEX/VIEW, or other ordinary SQL. "
                "Use separate INSERT statements or INSERT ALL for multi-row seed data; "
                "Oracle does not accept comma-separated VALUES tuples. Close every /* "
                "block comment with */ before the next executable statement. Every "
                "CREATE PROCEDURE/FUNCTION/TRIGGER must be complete through its outer "
                "END name; and SQL*Plus slash before the next requested section."
                if effective_sql_dialect == "oracle" else ""
            )
            language_repair_rules = (
                f"\nSQL validation rules: The authoritative dialect is "
                f"{effective_sql_dialect}. Preserve that one dialect and do not mix "
                "PostgreSQL PL/pgSQL, Oracle PL/SQL, T-SQL, or MySQL procedural syntax. "
                "For stored routines, use the delimiter, block syntax, exception syntax, "
                "and parameter conventions of the selected dialect. Procedure/function "
                "parameters must have names distinct from table columns. Qualify columns "
                "with table aliases in predicates; never emit a tautology such as "
                "`WHERE username = username`. Return one complete executable SQL file "
                f"without markdown fences or prose.{oracle_package_rules}\n"
            )
        if validation_language == "cobol":
            format_rule = (
                "Return IBM Enterprise COBOL fixed-format source: columns 1-6 blank; column 7 "
                "indicator only; Area A columns 8-11; Area B columns 12-72; no tabs or text "
                "after column 72; do not emit >>SOURCE FORMAT FREE."
                if cobol_fixed else
                "Return portable GnuCOBOL source beginning with `>>SOURCE FORMAT FREE`."
            )
            language_repair_rules = (
                f"\nCOBOL compiler rules: {format_rule} Remove the entire CONFIGURATION SECTION unless it is "
                "functionally required. Never emit CRT., OPERATING-SYSTEM., "
                "USER-IDENTIFICATION., IBM-370 declarations, or an empty SPECIAL-NAMES paragraph. "
                "For sequential files, ENVIRONMENT DIVISION should proceed directly to "
                "INPUT-OUTPUT SECTION and FILE-CONTROL. Every SELECT must have a matching "
                "FD and 01 record. Put FILE STATUS on SELECT and test the PIC XX status in "
                "separate IF statements after I/O. OPEN and CLOSE do not support ON ERROR; "
                "READ uses AT END / NOT AT END / END-READ and does not support ON ERROR; "
                "DISPLAY does not support WITH STATUS. Do not put a period inside an inline "
                "PERFORM before END-PERFORM, and never GO TO a SECTION.\n"
            )
        if validation_language == "cobol":
            fix_prompt = (
                "Rewrite this complete COBOL file so GnuCOBOL accepts it. The compiler errors "
                "are authoritative. You may rewrite any invalid paragraph; do not preserve "
                "syntax named by an error.\n\n"
                f"COMPILER ERRORS:\n{diagnostics_block}\n"
                f"{language_repair_rules}\n"
                "MANDATORY REPLACEMENTS:\n"
                "- Replace `DISPLAY message WITH STATUS status-name` with "
                "`DISPLAY message status-name`.\n"
                "- OPEN and CLOSE take the SELECT logical file name, never a quoted filename.\n"
                "- WRITE takes the FD record name, never a filename.\n"
                "- Every SELECT logical name must match an FD logical name.\n"
                "- PROGRAM-ID must be 30 characters or fewer.\n"
                "- Quote literal filenames in ASSIGN; never emit an unquoted dotted filename.\n"
                "- Omit ACCESS MODE for sequential files and do not put RECORDING MODE on SELECT.\n"
                "- Preserve a complete source file through PROCEDURE DIVISION and STOP RUN.\n"
                "- Return the entire corrected file, not a patch.\n\n"
                f"CURRENT FILE:\n{content}\n\n"
                "Return the complete corrected file using the same governed "
                "<generated_file> and <generation_notes> response envelope."
            )
        elif java_file_identity_mismatch:
            normalized_java_path = rel_path.replace("\\", "/")
            expected_type = Path(rel_path).stem
            expected_package = ""
            if "/src/main/java/" in normalized_java_path:
                expected_package = (
                    normalized_java_path.split("/src/main/java/", 1)[1]
                    .rsplit("/", 1)[0]
                    .replace("/", ".")
                )
            package_rule = (
                f"Its package declaration must be exactly `package {expected_package};`.\n"
                if expected_package else ""
            )
            fix_prompt = (
                "AUTHORITATIVE JAVA FILE IDENTITY (highest priority):\n"
                f"- Output path: {rel_path}\n"
                f"- The filename-matching top-level type must be named {expected_type}.\n"
                f"- {package_rule}"
                "- Emit exactly one public top-level type. Constructors and self-references must "
                f"also use {expected_type}.\n"
                "- Source evidence may contain legacy or neighboring type/package names; use it "
                "only for behavior and fields, never as the output file identity.\n\n"
                f"ORIGINAL FILE CONTRACT:\n{prompt}\n\n"
                "--- PREVIOUS ATTEMPT VIOLATED JAVA FILE IDENTITY ---\n"
                f"Issues found:\n{diagnostics_block}\n\n"
                f"{language_repair_rules}\n"
                "Discard the previous file completely and regenerate from the authoritative "
                "identity and original contract. Do not quote or preserve the previous wrong "
                "class, interface, record, enum, package, or table identity. Return the complete "
                "corrected Java source only, without markdown fences."
            )
        else:
            truncation_instruction = (
                "\nTRUNCATION RECOVERY: The previous artifact ended before its syntax tree "
                "closed. Treat it as incomplete evidence, not as a tail to patch. Rewrite the "
                "entire file from the beginning, preserve its required behavior, keep the "
                "implementation concise, and finish every declaration and JSX tree within this "
                "response. Do not append only closing tokens.\n"
                if truncation_recovery else ""
            )
            jsx_role_mismatch = bool(
                forbid_jsx
                and any("TS17004" in str(item) for item in result.diagnostics)
            )
            incomplete_plain_ts = bool(
                forbid_jsx
                and result.checker == "source-completeness"
                and any("incomplete placeholder construct" in str(item) for item in result.diagnostics)
            )
            if jsx_role_mismatch or incomplete_plain_ts or truncation_recovery:
                # Repeating the invalid component in the repair prompt anchors
                # small models to its JSX. The same applies to a placeholder
                # body or an EOF-shaped/truncated file. In all three cases,
                # embedding the failed source and saying "keep structure"
                # contradicts the required complete rewrite. Re-author from
                # the authoritative file contract without that anchor.
                if jsx_role_mismatch:
                    failure_heading = "PREVIOUS ATTEMPT USED THE WRONG FILE ROLE"
                    recovery_rule = (
                        "Discard the previous React/JSX attempt completely. Re-author this file "
                        "from the FILE CONTRACT as a plain .ts module. Do not preserve or quote "
                        "the previous component structure."
                    )
                elif incomplete_plain_ts:
                    failure_heading = "PREVIOUS ATTEMPT WAS AN INCOMPLETE TRANSPORT STUB"
                    recovery_rule = (
                        "Discard the previous placeholder implementation completely. Re-author "
                        "the full plain .ts module from the FILE CONTRACT. Every exported API "
                        "operation must return its declared source-evidenced value or throw a "
                        "descriptive error. Never return null as a stub; use no-value returns "
                        "only for operations explicitly declared as Promise<void>."
                    )
                else:
                    failure_heading = "PREVIOUS ATTEMPT WAS STRUCTURALLY INCOMPLETE"
                    recovery_rule = (
                        "Discard the structurally incomplete attempt completely. Re-author the "
                        "entire file from the FILE CONTRACT, keep the implementation concise, "
                        "include every required export, and close every expression and declaration "
                        "before the end of the response. Do not append only closing tokens."
                    )
                fix_prompt = (
                    f"{prompt}\n\n"
                    f"--- {failure_heading} ---\n"
                    f"Validator: {result.checker}\n"
                    f"Issues found:\n{diagnostics_block}\n\n"
                    f"{language_repair_rules}{truncation_instruction}\n"
                    f"{recovery_rule} Output the COMPLETE corrected file "
                    "using the same governed <generated_file> and <generation_notes> response "
                    "envelope. No markdown fences."
                )
            else:
                fix_prompt = (
                    f"{prompt}\n\n"
                    "--- PREVIOUS ATTEMPT FAILED VALIDATION ---\n"
                    f"Validator: {result.checker}\n"
                    f"Issues found:\n{diagnostics_block}\n\n"
                    f"{language_repair_rules}{truncation_instruction}\n"
                    "--- PREVIOUS ATTEMPT CONTENT ---\n"
                    f"{content}\n\n"
                    "Fix ONLY the reported issues. Keep all other names, structure, and logic unchanged. "
                    "Output the COMPLETE corrected file using the same governed "
                    "<generated_file> and <generation_notes> response envelope. No markdown fences."
                )
        fix_num_ctx = _adaptive_num_ctx(
            len(fix_prompt) + len(system or ""), repair_max_tokens,
        )
        if validation_language == "cobol":
            fix_num_ctx = min(fix_num_ctx, 8192)
        inference_started = time.monotonic()
        raw_response = generate(
            fix_prompt, model=repair_model, system=system, max_tokens=repair_max_tokens,
            num_ctx=fix_num_ctx, on_token=on_repair_token or on_token,
            think=False if validation_language == "cobol" else None,
            max_seconds=generation_budget(),
        )
        content = _clean_generated_content(
            parse_generation_response(raw_response, rel_path)
            if require_governance_envelope or raw_response.lstrip().startswith("<generated_file>")
            else raw_response
        )
        if language in {"typescript", "javascript"} or Path(rel_path).suffix.casefold() in {".ts", ".tsx", ".js", ".jsx"}:
            content = _repair_typescript_source(content)
        if language == "sql":
            content = _normalize_oracle_package_terminators(content, dialect)
            content = _repair_oracle_multirow_values(content, dialect)
            content = _repair_oracle_bind_variable_views(content, dialect)
        if language == "java":
            content = _normalize_jakarta_namespace(content)
            logger.info(
                "Java generation timing path=%s attempt=%d inference=%.3fs prompt_chars=%d output_chars=%d",
                rel_path, attempt, time.monotonic() - inference_started, len(fix_prompt), len(content),
            )
        validation_language = _detect_single_file_language(content, validation_language) if detect_language else language
        validation_path = (
            f"generated{_single_file_extension(validation_language, content)}"
            if detect_language else rel_path
        )
        validation_started = time.monotonic()
        result = validate_file(validation_path, content, validation_language, dialect_hint=dialect)
        content, result = deterministic_source_repair(content, result)
        result = completeness_gate(content, result)
        response_notes = generation_notes_snapshot().get(rel_path, {})
        if require_governance_envelope and not response_notes.get("format_valid"):
            result = type(result)(
                result.path, result.language, "generation-envelope", False,
                [*result.diagnostics, str(response_notes.get("error") or "missing governed generation notes")],
            )
        if language == "java":
            logger.info(
                "Java generation timing path=%s attempt=%d validation=%.3fs passed=%s diagnostics=%d total=%.3fs",
                rel_path, attempt, time.monotonic() - validation_started, result.passed,
                len(result.diagnostics), time.monotonic() - generation_started,
            )
        if (
            language in {"java", "csharp"}
            and not result.passed
            and _validation_diagnostic_fingerprint(result.diagnostics) == _pre_repair_diagnostics
            and not _truncation_shaped_diagnostics(result.diagnostics)
            and not any(
                "Broad Exception/Throwable catches are forbidden" in str(item)
                for item in result.diagnostics
            )
            and not java_file_identity_mismatch
        ):
            # Repeating byte-for-byte identical compiler diagnostics means the
            # repair did not improve this file. Another nearly identical prompt
            # burns a full local inference call without a useful new signal.
            # Apply this convergence check only to Java and C#, whose compiler
            # validators provide stable, source-specific diagnostics. TypeScript
            # repair responses can expose a later parser error with the same TS
            # code, so they must receive their configured final attempt.
            break

    if not result.passed:
        excerpt = _validation_failure_excerpt(content, result.diagnostics)
        logger.error(
            "Generated source validation exhausted path=%s language=%s attempts=%d "
            "diagnostics=%s source_excerpt=%s",
            rel_path, validation_language, attempt, result.diagnostics[:8], excerpt,
        )
    return content, result, attempt


# Function: _detect_single_file_language
def _detect_single_file_language(content: str, requested_language: str) -> str:
    """Route standalone output through the shared, content-aware detector."""
    if (requested_language or "").casefold() == "svg" and re.search(
        r"(?is)^\s*(?:<\?xml[^>]*>\s*)?<svg\b", content or "",
    ):
        return "svg"
    from services.validators import detect_source_language
    return detect_source_language(content, requested_language)


# Function: _generation_priority
def _generation_priority(path: str) -> tuple:
    """Generate contracts before their implementations and callers."""
    low = path.lower()
    if any(x in low for x in ("/entities/", "/models/", "/dtos/", "/domain/")):
        rank = 0
    elif low.endswith((".csproj", ".sln", "package.json", "angular.json", "tsconfig.json")):
        rank = 1
    elif any(x in low for x in ("repository", "service")) and "test" not in low:
        rank = 2 if re.search(r"/i[a-z]+(?:repository|service)", low) else 3
    elif any(x in low for x in ("controller", "component", "program.cs", "app.module")):
        rank = 4
    elif "test" in low or ".spec." in low:
        rank = 6
    else:
        rank = 5
    return rank, low


# Function: _prune_plan_for_baseline
def _prune_plan_for_baseline(
    planned: list[str],
    baseline: list[str],
) -> list[str]:
    """Remove model-proposed aliases that would duplicate baseline-owned types."""
    baseline_lower = {path.lower() for path in baseline}
    baseline_basenames = {Path(path).name.lower() for path in baseline}
    baseline_stems = {Path(path).stem.lower().lstrip("i") for path in baseline}
    baseline_has_tests = any(
        "test" in path.lower() or ".spec." in path.lower() for path in baseline
    )
    result: list[str] = []
    for path in planned:
        low = path.lower()
        base = Path(path).name.lower()
        stem = Path(path).stem.lower().lstrip("i")
        if low in baseline_lower or base in baseline_basenames:
            continue
        # Models/DTOs/entities and interfaces frequently get proposed twice in
        # different folders or with accidental names such as IIRepository.
        if stem in baseline_stems and any(
            marker in low
            for marker in ("/model", "/entit", "/dto", "repository", "service")
        ):
            continue
        if baseline_has_tests and ("test" in low or ".spec." in low):
            continue
        result.append(path)
    return result


# ---------------------------------------------------------------------------
# generate_from_prompt helpers — extracted phases of the prompt→project
# pipeline. Split out so each phase is independently readable/testable and
# to keep generate_from_prompt itself a thin sequence of phase calls.
# ---------------------------------------------------------------------------

_PROD_RULES_SINGLE_FILE = (
    "PRODUCTION CODE RULES (mandatory):\n"
    "- Complete implementation — no empty bodies, no TODO/FIXME stubs\n"
    "- All imports/package declarations at top of file\n"
    "- Full error handling with logging\n"
    "- Input validation on all public entry points\n"
    "- Configuration from environment variables, never hardcoded\n"
    "- No markdown fences (```) in the output"
)

_PROD_RULES_INLINE = (
    "PRODUCTION CODE RULES (mandatory for every file generated):\n"
    "- Complete implementation of every method — no empty bodies, no TODO/FIXME stubs\n"
    "- All imports, using statements, package declarations included\n"
    "- Full error handling: exceptions caught, logged, and re-raised or HTTP-mapped\n"
    "- Input validation on all public entry points\n"
    "- Structured logging in service/controller layers\n"
    "- Configuration (DB URLs, secrets, ports) from environment variables, never hardcoded\n"
    "- Async/await patterns where the framework supports them\n"
    "- No markdown code fences (```) in the output\n"
    "CONSISTENCY (this is where multi-file generation usually fails):\n"
    "- Never create a second file, type, or endpoint that does the same job as one already in "
    "the file plan, and never use a near-duplicate/typo-variant name for the same thing\n"
    "- Names must match across layers exactly: a route a client calls must exist on the server "
    "at that exact path; a call to X.Y(a, b) requires method Y on type X with that exact arity\n"
    "- Use only APIs, methods, and overloads that actually exist in the stated library versions. "
    "If unsure whether an API exists, use the standard documented one — never invent a method "
    "name, parameter, or overload\n"
    "- Keep names, casing, and serialization consistent end to end (e.g. if the API returns "
    "camelCase JSON, the client models are camelCase)\n"
    "- Handle the real edge cases the task implies (empty input, not-found, concurrency/atomicity, "
    "failure/rollback) — not just the happy path"
)
