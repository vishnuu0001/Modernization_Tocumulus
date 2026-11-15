"""Governed requirements, guideline, standards, and generation-audit services.

The module is deliberately independent from the LLM transport. It creates immutable
artifacts before generation and validates model accountability after generation.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from collections.abc import Iterable, Mapping, Sequence


_ROOT = Path(__file__).resolve().parents[1]
_CONFIG_ROOT = _ROOT / "config" / "generation"
_PACK_ROOT = _CONFIG_ROOT / "language-packs"
_ACCEPTANCE_POLICY_PATH = _CONFIG_ROOT / "language-acceptance.json"
_PROMPT_ROOT = _CONFIG_ROOT / "prompts"

# One authoritative manifest-ID registry shared by prompt injection, file
# planning, accountability validation, and traceability reporting. Keeping
# separate category lists caused valid NFR-* claims advertised to the model to
# be rejected later by the audit phase as unknown IDs.
_MANIFEST_ID_CATEGORIES = (
    "inputs",
    "outputs",
    "entities",
    "use_cases",
    "roles_permissions",
    "integrations",
    "non_functional_requirements",
    "deliverables",
)

_TRACE_REQUIREMENTS = {
    "inputs": ("input", {"capture", "validate", "audit"}),
    "outputs": ("output", {"produce", "render", "error-state"}),
    "entities": ("entity", {"persist", "test"}),
    "use_cases": ("use_case", {"implement", "test"}),
    "roles_permissions": ("role_permission", {"authorize", "test"}),
    "integrations": ("integration", {"configure", "test"}),
    "non_functional_requirements": ("non_functional_requirement", {"implement", "test"}),
    "deliverables": ("deliverable", {"produce"}),
}

_SOURCE_EXTENSIONS = {
    ".c", ".cc", ".clj", ".cob", ".cpp", ".cs", ".dart", ".erl", ".ex",
    ".exs", ".go", ".hs", ".java", ".js", ".jsx", ".jl", ".kt", ".kts",
    ".lisp", ".php", ".py", ".r", ".rb", ".rs", ".scala", ".sh", ".sql",
    ".swift", ".ts", ".tsx",
}
_CONFIG_FILENAMES = {
    ".editorconfig", ".prettierrc", ".prettierrc.json", ".eslintrc",
    ".eslintrc.json", "eslint.config.js", "eslint.config.mjs", "ruff.toml",
    "pyproject.toml", "mypy.ini", "setup.cfg", "checkstyle.xml", "spotbugs.xml",
    "stylecop.json", ".clang-format", ".golangci.yml", "detekt.yml", "phpstan.neon",
}
_CONFIG_EXTENSIONS = {
    ".env", ".ini", ".json", ".properties", ".toml", ".xml", ".yaml", ".yml",
}
_EXTENSION_LANGUAGES = {
    ".c": "c", ".cc": "cpp", ".clj": "clojure", ".cob": "cobol",
    ".cpp": "cpp", ".cs": "csharp", ".dart": "dart", ".erl": "erlang",
    ".ex": "elixir", ".exs": "elixir", ".go": "go", ".hs": "haskell",
    ".java": "java", ".js": "javascript", ".jsx": "javascript", ".jl": "julia",
    ".kt": "kotlin", ".kts": "kotlin", ".lisp": "lisp", ".md": "markdown",
    ".php": "php", ".proto": "protobuf", ".py": "python", ".r": "r",
    ".rb": "ruby", ".rs": "rust", ".scala": "scala", ".sh": "shell",
    ".sql": "sql", ".swift": "swift", ".toml": "toml", ".ts": "typescript",
    ".tsx": "typescript", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml",
    ".json": "json",
}
_PLACEHOLDER_RE = re.compile(
    r"(?im)(?:\bTODO\b|\bFIXME\b|\bNotImplemented(?:Error|Exception)?\b|"
    r"throw\s+new\s+UnsupportedOperationException\s*\(|"
    r"\breturn\s+(?:null|None|undefined)\s*;?\s*(?://.*)?$|"
    r"^\s*(?:pass|\.\.\.)\s*(?:#.*)?$|rest\s+of\s+(?:the\s+)?code|"
    r"implementation\s+(?:omitted|goes\s+here)|placeholder\s+(?:implementation|return))"
)


def _allowed_python_noop_lines(content: str) -> set[int]:
    """Return Python no-op lines that are complete language constructs.

    ``pass``/ellipsis is incomplete in an ordinary function body, but is valid
    for declarative marker classes, exception types, abstract methods and
    overload declarations. A no-op after another statement is redundant, not
    an empty implementation, and must not fail an otherwise complete file.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()
    allowed: set[int] = set()
    def is_noop(item: ast.AST) -> bool:
        return isinstance(item, ast.Pass) or (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and item.value.value is Ellipsis
        )

    def is_docstring(item: ast.AST) -> bool:
        return (
            isinstance(item, ast.Expr)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        )

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        noops = [item for item in body if is_noop(item)]
        if not noops:
            continue
        meaningful = [item for item in body if not is_noop(item) and not is_docstring(item)]
        if meaningful:
            allowed.update(item.lineno for item in noops)
            continue
        if isinstance(node, ast.ClassDef):
            base_names = {
                (base.id if isinstance(base, ast.Name) else base.attr)
                for base in node.bases
                if isinstance(base, (ast.Name, ast.Attribute))
            }
            marker_bases = {"DeclarativeBase", "Protocol", "TypedDict", "ABC"}
            if any(name.endswith(("Error", "Exception")) for name in base_names) \
                    or base_names.intersection(marker_bases):
                allowed.update(item.lineno for item in noops)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorator_names = {
                decorator.id if isinstance(decorator, ast.Name) else decorator.attr
                for decorator in node.decorator_list
                if isinstance(decorator, (ast.Name, ast.Attribute))
            }
            if decorator_names.intersection({"abstractmethod", "overload"}):
                allowed.update(item.lineno for item in noops)
    return allowed


def _allowed_jsx_null_return_lines(content: str, path: str) -> set[int]:
    """Allow intentional React guard branches without admitting null stubs.

    A React component or render helper may legitimately render nothing for a
    guarded state (for example ``if (!visible) return null``). Treating every
    ``return null`` as an incomplete implementation caused otherwise complete
    TSX domain generation to exhaust all repair attempts. The exemption is
    deliberately narrow: the file must be JSX/TSX, must contain a separate
    concrete JSX return, and the null return must be controlled by an ``if``.
    An unconditional component whose only implementation is ``return null``
    and ordinary TypeScript service methods remain blocked.
    """
    if PurePosixPath(path).suffix.casefold() not in {".jsx", ".tsx"}:
        return set()
    if not re.search(r"(?m)\breturn\s*(?:<|\(\s*<)", content):
        return set()

    lines = content.splitlines()
    allowed: set[int] = set()
    for match in re.finditer(r"(?im)\breturn\s+null\s*;?\s*(?://.*)?$", content):
        line_number = content.count("\n", 0, match.start()) + 1
        line_index = line_number - 1
        line_prefix = lines[line_index][:match.start() - (content.rfind("\n", 0, match.start()) + 1)]
        if re.search(r"\bif\s*\(", line_prefix):
            allowed.add(line_number)
            continue

        # Multiline guard form: find the nearest opening block in a small,
        # local window. It must belong to an if-condition, not the component's
        # function body.
        window_start = max(0, line_index - 8)
        prior = lines[window_start:line_index]
        nearest_open = next(
            (index for index in range(len(prior) - 1, -1, -1) if "{" in prior[index]),
            None,
        )
        if nearest_open is None:
            continue
        guard_text = "\n".join(prior[max(0, nearest_open - 5):])
        if re.search(r"(?s)\bif\s*\(.*\)\s*\{\s*$", guard_text):
            allowed.add(line_number)
    return allowed


_TS_NULLABLE_FUNCTION_PATTERNS = (
    re.compile(
        r"(?ms)(?:^|\n)[ \t]*(?:(?:export|default|public|private|protected|static|async)\s+)*"
        r"(?:function\s+)?[A-Za-z_$][\w$]*\s*\([^;{}]*\)\s*:\s*"
        r"(?P<return>[^\n{]+?)\s*\{"
    ),
    re.compile(
        r"(?ms)(?:^|\n)[ \t]*(?:export\s+)?(?:const|let)\s+[A-Za-z_$][\w$]*"
        r"(?:\s*:[^=\n]+)?\s*=\s*(?:async\s+)?\([^;{}]*\)\s*:\s*"
        r"(?P<return>[^\n{]+?)\s*=>\s*\{"
    ),
)


def _return_is_if_guarded(content: str, match: re.Match) -> bool:
    """Return whether a null return is controlled by a local if branch."""
    lines = content.splitlines()
    line_number = content.count("\n", 0, match.start()) + 1
    line_index = line_number - 1
    line_start = content.rfind("\n", 0, match.start()) + 1
    if re.search(r"\bif\s*\(", lines[line_index][:match.start() - line_start]):
        return True
    window_start = max(0, line_index - 8)
    prior = lines[window_start:line_index]
    nearest_open = next(
        (index for index in range(len(prior) - 1, -1, -1) if "{" in prior[index]),
        None,
    )
    if nearest_open is None:
        return False
    guard_text = "\n".join(prior[max(0, nearest_open - 5):])
    return bool(re.search(r"(?s)\bif\s*\(.*\)\s*\{\s*$", guard_text))


def _allowed_typed_nullable_null_return_lines(content: str, path: str) -> set[int]:
    """Allow guarded null results promised by an explicit TypeScript type.

    A transport lookup commonly maps an HTTP 404 to ``null`` under a declared
    ``Promise<Entity | null>`` contract. That is a complete result branch, not
    a placeholder. Keep the exemption narrow: plain TypeScript only, an
    explicit nullable return annotation, an enclosing function body, and a
    local ``if`` guard are all required. Unconditional/null-only functions
    therefore continue to fail governance.
    """
    if PurePosixPath(path).suffix.casefold() != ".ts":
        return set()
    functions: list[tuple[int, bool]] = []
    for pattern in _TS_NULLABLE_FUNCTION_PATTERNS:
        for function_match in pattern.finditer(content):
            return_type = function_match.group("return")
            functions.append((
                function_match.end() - 1,
                bool(re.search(r"\bnull\b", return_type, re.IGNORECASE)),
            ))
    if not functions:
        return set()

    allowed: set[int] = set()
    for match in re.finditer(r"(?im)\breturn\s+null\s*;?\s*(?://.*)?$", content):
        if not _return_is_if_guarded(content, match):
            continue
        for body_open, is_nullable in sorted(functions, reverse=True):
            if body_open >= match.start():
                continue
            # Braces inside expressions remain balanced; a positive depth at
            # the return proves this candidate function has not closed yet.
            body_prefix = content[body_open:match.start()]
            if body_prefix.count("{") > body_prefix.count("}"):
                if is_nullable:
                    allowed.add(content.count("\n", 0, match.start()) + 1)
                break
    return allowed


def _first_placeholder(content: str, path: str):
    allowed_pass_lines = (
        _allowed_python_noop_lines(content)
        if PurePosixPath(path).suffix.casefold() == ".py" else set()
    )
    allowed_jsx_null_lines = _allowed_jsx_null_return_lines(content, path)
    allowed_typed_nullable_null_lines = _allowed_typed_nullable_null_return_lines(
        content, path,
    )
    for match in _PLACEHOLDER_RE.finditer(content):
        line = content.count("\n", 0, match.start()) + 1
        token = match.group(0).strip().split("#", 1)[0].strip()
        if token in {"pass", "..."} and line in allowed_pass_lines:
            continue
        if re.fullmatch(r"return\s+null\s*;?(?:\s*//.*)?", token, re.IGNORECASE) \
                and line in allowed_jsx_null_lines | allowed_typed_nullable_null_lines:
            continue
        return match
    return None


@dataclass(frozen=True)
class StructuredSplitRequest(Exception):
    """A model declined truncation and requested complete replacement files."""

    source_path: str
    files: tuple[Mapping[str, str], ...]
    reason: str

    def __str__(self) -> str:
        return f"Structured split requested for {self.source_path}: {self.reason}"


_NOTES_LOCK = threading.RLock()
_FILE_NOTES: dict[str, dict[str, Any]] = {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _checksum(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def load_prompt_template(name: str) -> str:
    """Load an editable prompt template and fail closed when it is absent."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise ValueError(f"Invalid prompt template name: {name!r}")
    path = _PROMPT_ROOT / f"{name}.md"
    if not path.is_file():
        raise RuntimeError(f"Required generation prompt template is missing: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError(f"Required generation prompt template is empty: {path}")
    return text.strip()


def _section_items(text: str, aliases: Sequence[str]) -> list[str]:
    alias_expr = "|".join(re.escape(value) for value in aliases)
    heading = re.search(
        rf"(?im)^\s*(?:[^\r\n]*?[.!?]\s+)?(?:#{{1,6}}\s*)?(?:{alias_expr})\s*:?[ \t]*$",
        text,
    )
    body = ""
    if heading:
        remainder = text[heading.end():]
        stop = re.search(r"(?m)^\s*(?:#{1,6}\s+|[A-Z][A-Za-z /_-]{2,}:\s*$)", remainder)
        body = remainder[: stop.start()] if stop else remainder
    else:
        inline = re.search(rf"(?im)^\s*(?:{alias_expr})\s*:\s*(.+)$", text)
        body = inline.group(1) if inline else ""
    items: list[str] = []
    for raw in body.splitlines():
        value = re.sub(r"^\s*(?:[-*+] |\d+[.)]\s+)", "", raw).strip()
        if value:
            items.append(value)
    return items


def _markdown_sections(text: str) -> list[tuple[str, str]]:
    """Return Markdown sections without losing tables, lists or fenced trees."""
    headings = list(re.finditer(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", text))
    sections: list[tuple[str, str]] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        title = re.sub(r"[*_`]", "", heading.group(1)).strip()
        sections.append((title, text[heading.end():end]))
    return sections


def _markdown_list_items(body: str) -> list[str]:
    items: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^\s*(?:[-*+] |\d+[.)]\s+)(.+?)\s*$", line)
        if not match:
            continue
        value = re.sub(r"\s+", " ", match.group(1)).strip()
        if value and not value.startswith(("#", "|")):
            items.append(value)
    return items


def _dedupe_verbatim(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = re.sub(r"\s+", " ", str(value)).strip(" -\t\r\n")
        key = re.sub(r"[^a-z0-9]+", " ", cleaned.casefold()).strip()
        if cleaned and key and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _endpoint_requirements(text: str) -> list[tuple[str, str, str]]:
    endpoints: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        cells = [re.sub(r"[*_`]", "", value).strip() for value in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        method = cells[0].upper()
        route = cells[1]
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            continue
        if not route.startswith("/"):
            continue
        endpoints.append((method, route, cells[2] if len(cells) > 2 else "API operation"))
    return list(dict.fromkeys(endpoints))


def _prompt_structure(text: str) -> dict[str, list[str]]:
    """Extract explicit requirements from ordinary long-form Markdown prompts.

    This is deterministic on purpose: a requirements manifest must not depend
    on an LLM deciding whether a table, numbered list, or deliverables section
    is important enough to retain.
    """
    sections = _markdown_sections(text)
    endpoints = _endpoint_requirements(text)
    inputs = [
        f"{method} {route}: validated path, query, header and request-body fields required by this operation"
        for method, route, _ in endpoints
    ]
    outputs = [f"{method} {route}: {purpose}" for method, route, purpose in endpoints]
    entities: list[str] = []
    use_cases: list[str] = [f"{method} {route}: {purpose}" for method, route, purpose in endpoints]
    integrations: list[str] = []
    nfrs: list[str] = []
    deliverables: list[str] = []
    roles: list[str] = []

    for match in re.finditer(
        r"(?im)(?:schema\s+(?:containing|includes?)\s*:|\bentities\b\s*:)\s*\r?\n+"
        r"((?:[ \t]*[-*+]\s+[^\r\n]+\r?\n?)+)",
        text,
    ):
        entities.extend(_markdown_list_items(match.group(1)))

    # Database-only prompts commonly describe tables under a Context/My
    # tables section instead of an Entities heading. Preserve explicit SQL
    # identifiers and their column lists without mistaking ordinary prose
    # bullets such as "Amount convention" for schema objects.
    for match in re.finditer(
        r"(?m)^\s*-\s*([A-Z][A-Z0-9_$#]{1,62})\s*:\s*\[([^\]\r\n]+)\]\s*$",
        text,
    ):
        entities.append(f"{match.group(1)}: {match.group(2).strip()}")

    for title, body in sections:
        key = title.casefold()
        items = _markdown_list_items(body)
        if any(token in key for token in ("core", "feature", "functional", "workflow", "use case")):
            use_cases.extend(items)
        if any(token in key for token in ("transfer engine", "processing workflow", "business process")):
            use_cases.extend(items)
        if any(token in key for token in ("expected deliverable", "deliverable", "required artifact", "outputs")):
            deliverables.extend(items)
        if any(token in key for token in ("integration", "external system", "infrastructure")):
            integrations.extend(items)
        if any(token in key for token in (
            "security", "testing", "observability", "compliance", "container", "deployment",
            "ci/cd", "non-functional", "quality", "implementation instruction",
        )):
            nfrs.extend(items)
        if any(token in key for token in ("role", "permission", "actor")):
            roles.extend(items)
        if any(token in key for token in ("data model", "domain entit", "database schema")):
            entities.extend(items)

    # Domain responsibility prose commonly names the aggregate types even
    # when the author did not add a dedicated "Entities" heading.
    for match in re.finditer(r"(?im)^\s*-\s*\*\*Domain:\*\*\s*(.+)$", text):
        entities.extend(part.strip(" .") for part in match.group(1).split(","))

    role_terms = re.findall(
        r"(?i)\b(?:administrator|administration|administrative|admin|auditor|operator|manager|customer|guest|user)s?\b",
        text,
    )
    normalized_roles = {
        "administration": "Administrator", "administrative": "Administrator",
        "admin": "Administrator", "administrator": "Administrator",
    }
    roles.extend(normalized_roles.get(term.casefold().rstrip("s"), term.title().rstrip("s")) for term in role_terms)
    return {
        "inputs": _dedupe_verbatim(inputs),
        "outputs": _dedupe_verbatim(outputs),
        "entities": _dedupe_verbatim(entities),
        "use_cases": _dedupe_verbatim(use_cases),
        "roles": _dedupe_verbatim(roles),
        "integrations": _dedupe_verbatim(integrations),
        "nfrs": _dedupe_verbatim(nfrs),
        "deliverables": _dedupe_verbatim(deliverables),
    }


def _record_name_and_description(value: str, fallback: str) -> tuple[str, str]:
    match = re.match(r"(?:`([^`]+)`|([A-Za-z][\w .-]{0,80}))\s*(?::| - | — )\s*(.+)", value)
    if not match:
        cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_")[:60]
        return cleaned or fallback, value
    return (match.group(1) or match.group(2)).strip(), match.group(3).strip()


def _infer_type(text: str) -> str:
    lowered = text.casefold()
    candidates = (
        ("decimal", ("decimal", "money", "amount", "currency", "numeric")),
        ("integer", ("integer", " int", "count", "number of")),
        ("boolean", ("boolean", "bool", "true/false")),
        ("uuid", ("uuid", "guid")),
        ("date-time", ("datetime", "date-time", "timestamp")),
        ("date", (" date", "date ")),
        ("email", ("email", "e-mail")),
        ("array", ("array", "list of", "collection")),
        ("object", ("object", "record", "payload")),
    )
    return next((kind for kind, needles in candidates if any(n in lowered for n in needles)), "string")


def _constraints(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {"verbatim": text}
    length = re.search(r"(?i)(?:max(?:imum)?\s*(?:length)?|length)\s*[:=]?\s*(\d+)", text)
    range_match = re.search(r"(?i)(?:between|range)\s+(-?\d+(?:\.\d+)?)\s+(?:and|to)\s+(-?\d+(?:\.\d+)?)", text)
    allowed = re.search(r"(?i)(?:allowed|one of|enum)\s*[:=]\s*([^.;]+)", text)
    format_match = re.search(r"(?i)(?:format|pattern|regex)\s*[:=]\s*([^.;]+)", text)
    if length:
        result["max_length"] = int(length.group(1))
    if range_match:
        result["minimum"], result["maximum"] = range_match.groups()
    if allowed:
        result["allowed_values"] = [v.strip(" `\"'") for v in re.split(r"[,|]", allowed.group(1)) if v.strip()]
    if format_match:
        result["format"] = format_match.group(1).strip()
    return result


def _input_record(index: int, value: str) -> dict[str, Any]:
    name, description = _record_name_and_description(value, f"input_{index}")
    required = not bool(re.search(r"(?i)\boptional\b|\bnullable\b", description))
    sensitivity = "sensitive" if re.search(
        r"(?i)password|secret|token|credential|ssn|personal|pii|health|card", value
    ) else "internal"
    return {
        "id": f"IN-{index:03d}", "name": name, "description": description,
        "type": _infer_type(value), "required": required, "constraints": _constraints(value),
        "sensitivity": sensitivity,
        "validation_message": f"{name} is invalid" if required else f"{name}, when supplied, is invalid",
    }


def _output_record(index: int, value: str) -> dict[str, Any]:
    name, description = _record_name_and_description(value, f"output_{index}")
    return {
        "id": f"OUT-{index:03d}", "name": name, "description": description,
        "shape": _infer_type(value), "fields": [], "format": _constraints(value).get("format", "application/json"),
        "states": ["success", "empty", "validation_error", "unauthorized", "forbidden", "not_found", "conflict", "partial", "unexpected_error"],
    }


def freeze_requirements_manifest(
    user_request: str,
    target: Mapping[str, Any],
    *,
    run_instructions: str = "",
) -> dict[str, Any]:
    """Create the frozen Phase-A requirements artifact before code generation."""
    if not user_request or not user_request.strip():
        raise ValueError("A non-empty user request is required")
    requirements_text = "\n".join(filter(None, (
        user_request.strip(), (run_instructions or "").strip(),
    )))
    structured = _prompt_structure(requirements_text)
    input_items = _dedupe_verbatim([
        *_section_items(requirements_text, ("inputs", "input fields", "request fields", "parameters")),
        *structured["inputs"],
    ])
    output_items = _dedupe_verbatim([
        *_section_items(requirements_text, ("outputs", "responses", "reports", "events", "return values")),
        *structured["outputs"],
    ])
    assumptions: list[dict[str, str]] = []
    if not input_items:
        input_items = ["request: validated object containing the explicitly described user request fields"]
        assumptions.append({"id": "A-001", "text": "No discrete input-field list was supplied; the request is represented as a validated object and must be refined into typed fields during contract planning."})
    if not output_items:
        output_items = ["result: operation result using the declared API or user-interface format"]
        assumptions.append({"id": f"A-{len(assumptions)+1:03d}", "text": "No discrete output list was supplied; the primary result uses the selected stack's standard structured response and all outcome states."})
    entities = _dedupe_verbatim([
        *_section_items(requirements_text, ("entities", "domain entities", "data model")),
        *structured["entities"],
    ])
    use_cases = _dedupe_verbatim([
        *_section_items(requirements_text, ("use cases", "workflows", "functional requirements")),
        *structured["use_cases"],
    ])
    roles = _dedupe_verbatim([
        *_section_items(requirements_text, ("roles", "permissions", "actors")),
        *structured["roles"],
    ])
    if not roles:
        roles = _dedupe_verbatim(re.findall(
            r"\b(?:ADMIN|[A-Z][A-Z0-9_]{2,}_USER|administrator|operator|user)\b",
            requirements_text, re.IGNORECASE,
        ))
    integrations = _dedupe_verbatim([
        *_section_items(requirements_text, ("integrations", "external systems", "dependencies")),
        *structured["integrations"],
    ])
    nfrs = _dedupe_verbatim([
        *_section_items(requirements_text, ("non-functional requirements", "quality attributes", "nfrs")),
        *structured["nfrs"],
    ])
    if not nfrs:
        nfrs = [
            "Deny-by-default authorization and least privilege",
            "Structured logs, correlation IDs, traces, metrics, and health probes",
            "Bounded timeouts and retries; idempotent transactional state changes",
            "Pinned dependencies, reproducible build, automated tests, and deployment documentation",
        ]
        assumptions.append({"id": f"A-{len(assumptions)+1:03d}", "text": "Conservative production non-functional defaults apply because no complete NFR section was supplied."})
    prompt_versions = re.findall(
        r"(?i)\b(?:\.NET|ASP\.NET\s+Core|Java|Python|Angular|React|Node(?:\.js)?|PostgreSQL|SQL\s+Server)\s+v?(\d+(?:\.\d+){0,2})\b",
        requirements_text,
    )
    stack = {
        "name": str(target.get("name") or "custom"),
        "language": str(target.get("language") or ""),
        "backend": str(target.get("backend_tech") or ""),
        "frontend": str(target.get("frontend_tech") or ""),
        "database": str(target.get("db_tech") or ""),
        "database_target": str(target.get("db_target") or ""),
        "declared_versions": sorted({
            *prompt_versions,
            *re.findall(r"\b\d+(?:\.\d+){0,2}\b", " ".join(str(v) for v in target.values())),
        }),
        "request_technologies": _dedupe_verbatim(re.findall(
            r"(?im)^\s*-\s+([^\n]{2,100}(?:version|\b\d+(?:\.\d+){0,2}\b|Angular|React|Dapper|Flyway|Bicep|Kubernetes|AKS|Docker|OpenTelemetry|Serilog)[^\n]*)$",
            requirements_text,
        )),
    }
    manifest: dict[str, Any] = {
        "schema_version": "1.0.0", "state": "frozen",
        "source": {"request_verbatim": user_request, "run_instructions_verbatim": run_instructions},
        "inputs": [_input_record(i, value) for i, value in enumerate(input_items, 1)],
        "outputs": [_output_record(i, value) for i, value in enumerate(output_items, 1)],
        "entities": [{"id": f"ENT-{i:03d}", "verbatim": value} for i, value in enumerate(entities, 1)],
        "use_cases": [{"id": f"UC-{i:03d}", "verbatim": value} for i, value in enumerate(use_cases, 1)],
        "roles_permissions": [{"id": f"ROLE-{i:03d}", "verbatim": value, "default": "deny"} for i, value in enumerate(roles, 1)],
        "integrations": [{"id": f"INT-{i:03d}", "verbatim": value} for i, value in enumerate(integrations, 1)],
        "non_functional_requirements": [{"id": f"NFR-{i:03d}", "verbatim": value} for i, value in enumerate(nfrs, 1)],
        "deliverables": [{"id": f"DEL-{i:03d}", "verbatim": value} for i, value in enumerate(structured["deliverables"], 1)],
        "stack": stack, "assumptions": assumptions,
    }
    manifest["checksum"] = _checksum(manifest)
    return manifest


def manifest_completeness_diagnostics(
    user_request: str, manifest: Mapping[str, Any],
) -> list[str]:
    """Prove Phase A retained every deterministically extractable requirement."""
    source = manifest.get("source") or {}
    authoritative_text = "\n".join(filter(None, (
        user_request or "",
        str(source.get("run_instructions_verbatim") or ""),
    )))
    structured = _prompt_structure(authoritative_text)
    category_map = {
        "inputs": "inputs",
        "outputs": "outputs",
        "entities": "entities",
        "use_cases": "use_cases",
        "roles": "roles_permissions",
        "integrations": "integrations",
        "nfrs": "non_functional_requirements",
        "deliverables": "deliverables",
    }
    diagnostics: list[str] = []
    for extracted_category, manifest_category in category_map.items():
        expected = len(structured.get(extracted_category) or [])
        actual = len(manifest.get(manifest_category) or [])
        if actual < expected:
            diagnostics.append(
                f"{manifest_category}: manifest retained {actual} of {expected} "
                "deterministically extractable requirements"
            )
    endpoints = _endpoint_requirements(authoritative_text)
    if endpoints:
        for category in ("inputs", "outputs", "use_cases"):
            actual = len(manifest.get(category) or [])
            if actual < len(endpoints):
                diagnostics.append(
                    f"{category}: {len(endpoints)} REST operations were supplied but the manifest "
                    f"contains only {actual} records"
                )
    return list(dict.fromkeys(diagnostics))


def assert_frozen_manifest(manifest: Mapping[str, Any]) -> None:
    if manifest.get("state") != "frozen":
        raise ValueError("Requirements Manifest is not frozen")
    copy = dict(manifest)
    expected = copy.pop("checksum", "")
    if not expected or _checksum(copy) != expected:
        raise ValueError("Requirements Manifest checksum mismatch")


def _split_documents(text: str) -> list[tuple[str, str]]:
    if not text.strip():
        return []
    matches = list(re.finditer(r"(?m)^===\s+(.+?)(?:\s+\[[^\]]+\])?\s+===\s*$", text))
    if not matches:
        return [("inline.md", text)]
    documents: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        documents.append((match.group(1).strip(), text[match.end():end].strip("\r\n")))
    return documents


def extract_attached_config_files(guideline_text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for filename, content in _split_documents(guideline_text):
        safe_name = PurePosixPath(filename.replace("\\", "/")).name
        if safe_name.casefold() in {name.casefold() for name in _CONFIG_FILENAMES}:
            result[safe_name] = content.rstrip() + "\n"
    return result


def _atomize_document(filename: str, content: str) -> list[str]:
    if filename.casefold() in {name.casefold() for name in _CONFIG_FILENAMES}:
        return [content] if content.strip() else []
    rules: list[str] = []
    paragraph: list[str] = []
    for line in content.splitlines():
        if not line.strip():
            if paragraph:
                rules.append("\n".join(paragraph).strip())
                paragraph = []
            continue
        if re.match(r"^\s*(?:[-*+] |\d+[.)]\s+)", line):
            if paragraph:
                rules.append("\n".join(paragraph).strip())
                paragraph = []
            rules.append(line.strip())
        elif line.lstrip().startswith("#"):
            if paragraph:
                rules.append("\n".join(paragraph).strip())
                paragraph = []
        else:
            paragraph.append(line.rstrip())
    if paragraph:
        rules.append("\n".join(paragraph).strip())
    return [rule for rule in rules if rule]


def _strip_platform_governance_evidence(content: str) -> str:
    """Keep review corrections, but never reinterpret platform evidence as rules."""
    markers = (
        "LOCKED CANONICAL CONTRACTS (all generated files must conform):",
        "AUTHORITATIVE PROJECT DETAILS captured at project creation",
    )
    if not any(marker in content for marker in markers):
        return content
    correction_marker = "MANDATORY REVIEW CORRECTIONS FOR THIS RUN:"
    if correction_marker in content:
        return content.split(correction_marker, 1)[1].strip()
    return ""


def _rule_scope(text: str) -> list[str]:
    lowered = text.casefold()
    mapping = {
        "frontend": ("frontend", "ui", "react", "angular", "browser", "component"),
        "backend": ("backend", "api", "controller", "service", "endpoint"),
        "db": ("database", "sql", "table", "migration", "repository"),
        "tests": ("test", "coverage", "mock", "fixture"),
        "iac": ("terraform", "bicep", "kubernetes", "helm", "docker", "pipeline", "deployment"),
    }
    scopes = [scope for scope, words in mapping.items() if any(word in lowered for word in words)]
    return scopes or ["all"]


def _rule_languages(text: str) -> list[str]:
    lowered = text.casefold()
    names = {
        "csharp": ("c#", ".net", "dotnet"), "java": ("java", "spring"),
        "typescript": ("typescript", "angular", "react"), "javascript": ("javascript", "node"),
        "python": ("python", "fastapi", "django"), "cobol": ("cobol",), "sql": ("sql",),
        "go": (" golang", "go "), "rust": ("rust",), "php": ("php", "laravel"),
        "ruby": ("ruby", "rails"), "kotlin": ("kotlin",), "shell": ("shell", "bash"),
    }
    return [name for name, words in names.items() if any(word in lowered for word in words)]


def _machine_check(text: str) -> dict[str, Any] | None:
    lowered = text.casefold()
    quoted = re.findall(r"`([^`]+)`|\"([^\"]+)\"|'([^']+)'", text)
    tokens = [next(value for value in group if value) for group in quoted]
    # Extract quoted values only from the actual prohibition clause. A frozen
    # JSON contract can contain a quoted user prompt which itself says "do not";
    # treating every other JSON key/value as banned made the governance bundle
    # prohibit its own schema, checksum and source evidence.
    prohibition_clauses = re.findall(
        r"(?is)\b(?:never|must\s+not|do\s+not|banned?|forbid(?:den)?)\b[^.;\r\n]*",
        text,
    )
    banned = []
    for clause in prohibition_clauses:
        values = re.findall(r"`([^`]+)`|\"([^\"]+)\"|'([^']+)'", clause)
        banned.extend(next(value for value in group if value) for group in values)
    if banned:
        return {"kind": "banned_tokens", "values": list(dict.fromkeys(banned))}
    pattern = re.search(r"(?i)(?:naming pattern|must match|regex)\s*[:=]\s*`?([^`\n]+)`?", text)
    if pattern:
        return {"kind": "path_pattern", "value": pattern.group(1).strip()}
    if "required header" in lowered and tokens:
        return {"kind": "required_header", "value": tokens[0]}
    frameworks = [name for name in ("xunit", "nunit", "junit", "pytest", "vitest", "jest", "testng") if name in lowered]
    if frameworks and "test" in lowered:
        return {"kind": "test_framework", "values": frameworks}
    return None


def atomize_guidelines(project_guidelines: str = "", run_instructions: str = "") -> dict[str, Any]:
    """Preserve supplied wording while assigning scope, precedence, and checks."""
    rules: list[dict[str, Any]] = []
    for prefix, source, text, default_priority in (
        ("G", "project", project_guidelines, 2),
        ("R", "run", run_instructions, 3),
    ):
        counter = 0
        for filename, content in _split_documents(text):
            if filename.casefold() not in {name.casefold() for name in _CONFIG_FILENAMES}:
                content = _strip_platform_governance_evidence(content)
            for wording in _atomize_document(filename, content):
                counter += 1
                security = bool(re.search(r"(?i)security|privacy|encrypt|secret|credential|auth|pii|gdpr|data protection", wording))
                rules.append({
                    "id": f"{prefix}-{counter:03d}", "source": source,
                    "source_file": filename, "verbatim": wording,
                    "precedence": 1 if security else default_priority,
                    "security_data_protection": security,
                    "layers": _rule_scope(wording), "languages": _rule_languages(wording),
                    "machine_check": _machine_check(wording),
                })
    conflicts = _detect_rule_conflicts(rules)
    result = {"schema_version": "1.0.0", "rules": rules, "conflicts": conflicts}
    result["checksum"] = _checksum(result)
    return result


def _detect_rule_conflicts(rules: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups = {
        "test_framework": ("xunit", "nunit", "mstest", "junit", "testng", "pytest", "unittest", "vitest", "jest"),
        "formatter": ("prettier", "biome", "black", "ruff format", "google java format", "spotless"),
        "data_access": ("entity framework", "dapper", "hibernate", "jooq", "sqlalchemy", "django orm"),
    }
    conflicts: list[dict[str, Any]] = []
    for category, choices in groups.items():
        selected: list[tuple[Mapping[str, Any], str]] = []
        for rule in rules:
            lowered = str(rule["verbatim"]).casefold()
            selected.extend((rule, choice) for choice in choices if choice in lowered)
        distinct = {choice for _, choice in selected}
        if len(distinct) < 2:
            continue
        winner, winner_choice = min(selected, key=lambda item: (int(item[0]["precedence"]), str(item[0]["id"])))
        conflicts.append({
            "category": category, "rule_ids": sorted({str(rule["id"]) for rule, _ in selected}),
            "choices": sorted(distinct), "winner_rule_id": winner["id"],
            "winner_value": winner_choice,
            "resolution": "highest-precedence rule wins; conflict retained in audit",
        })
    return conflicts


def applicable_rules(rules: Sequence[Mapping[str, Any]], path: str, language: str) -> list[dict[str, Any]]:
    lowered = path.casefold()
    name = PurePosixPath(lowered).name
    artifact_language = _EXTENSION_LANGUAGES.get(PurePosixPath(lowered).suffix, language)
    if name == "dockerfile":
        artifact_language = "dockerfile"
    elif name == "jenkinsfile":
        artifact_language = "jenkinsfile"
    layer = (
        "tests" if any(token in lowered for token in ("test", "spec")) else
        "iac" if any(token in lowered for token in ("docker", "k8s", "helm", "terraform", "bicep", "workflow")) else
        "db" if any(token in lowered for token in ("migration", "schema", ".sql", "repository")) else
        "frontend" if any(token in lowered for token in ("frontend", "component", ".tsx", ".jsx", ".vue", ".html")) else
        "backend"
    )
    return [dict(rule) for rule in rules if
            ("all" in rule.get("layers", []) or layer in rule.get("layers", [])) and
            (not rule.get("languages") or artifact_language in rule.get("languages", []))]


def load_language_standards(language: str, target: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Load only versioned packs relevant to the selected run."""
    language = (language or "").casefold()
    frontend = str(target.get("frontend_tech") or "").casefold()
    names: list[str] = []
    if language not in {"typescript", "javascript"}:
        artifact_languages = {
            "ansible", "cloudformation", "dockerfile", "github_actions", "graphql",
            "hcl", "helm", "jenkinsfile", "json", "kubernetes", "markdown",
            "protobuf", "toml", "xml", "yaml",
        }
        versioned_name = "iac" if language in artifact_languages else language
        if language == "csharp":
            runtime = " ".join(str(target.get(key) or "") for key in ("name", "backend_tech", "runtime"))
            dotnet = re.search(r"(?i)(?:\.NET|ASP\.NET\s+Core)\s*(\d+)", runtime)
            candidate = f"csharp-dotnet{dotnet.group(1)}" if dotnet else ""
            if candidate and (_PACK_ROOT / f"{candidate}.json").is_file():
                versioned_name = candidate
        names.append(versioned_name)
    if "angular" in frontend:
        names.append("typescript-angular")
    elif "react" in frontend:
        names.append("typescript-react")
    elif language in {"typescript", "javascript"}:
        names.append(language)
    if str(target.get("db_target") or "").casefold() in {"postgres", "mssql", "mysql", "oracle", "db2", "sqlite"}:
        names.append("sql")
    if target.get("deploy") or target.get("deployment_kind"):
        names.append("iac")
    packs: list[dict[str, Any]] = []
    for name in dict.fromkeys(names):
        path = _PACK_ROOT / f"{name}.json"
        if not path.is_file():
            raise RuntimeError(f"No editable language standards pack exists for supported language {name!r}")
        pack = json.loads(path.read_text(encoding="utf-8"))
        if not pack.get("version") or not pack.get("id"):
            raise RuntimeError(f"Language standards pack lacks id/version: {path}")
        pack = dict(pack)
        if language in {str(value).casefold() for value in pack.get("languages") or []}:
            pack["acceptance_policy"] = load_language_acceptance_policy(language)
        packs.append(pack)
    return packs


def render_governance_prompt(
    manifest: Mapping[str, Any],
    guideline_set: Mapping[str, Any],
    standards: Sequence[Mapping[str, Any]],
    *,
    path: str | None = None,
    language: str = "",
    all_requirements: bool = False,
) -> str:
    """Render the governance block for a prompt.

    ``all_requirements`` keeps the per-file envelope contract for ``path`` but
    shows every manifest ID. Single-file generation uses it because its one
    file is the entire deliverable; the lexical per-path slice otherwise told
    the model there were no IDs to account for while the audit required them.
    """
    assert_frozen_manifest(manifest)
    rules = list(guideline_set.get("rules") or [])
    if path:
        rules = applicable_rules(rules, path, language)
    prompt_manifest = _manifest_prompt_view(manifest, None if all_requirements else path)
    rules_text = "\n".join(f"{rule['id']} [precedence {rule['precedence']}]: {rule['verbatim']}" for rule in rules) or "(none supplied)"
    ids = [
        item["id"]
        for key in _MANIFEST_ID_CATEGORIES
        for item in prompt_manifest.get(key, [])
    ]
    return (
        "\n\nFROZEN REQUIREMENTS MANIFEST VIEW:\n" + json.dumps(prompt_manifest, ensure_ascii=False, indent=2) +
        "\n\nAPPLICABLE VERBATIM RULES:\n" + rules_text +
        "\n\nSELECTED VERSIONED LANGUAGE STANDARDS:\n" + json.dumps(list(standards), ensure_ascii=False, indent=2) +
        "\n\nACCOUNTABILITY: Return applicable rule IDs and genuinely covered manifest IDs. "
        f"Known manifest IDs: {', '.join(ids)}.\n" +
        (load_prompt_template("per-file") if path else
         "The per-file source envelope applies only during file-generation calls, not during planning.")
    )


_MAPPING_STOP_WORDS = {
    "with", "from", "that", "this", "using", "required", "application",
    "system", "complete", "implementation", "provide", "create", "build",
}


def _requirement_ids_for_path(path: str, manifest: Mapping[str, Any]) -> list[str]:
    """Return the deterministic, file-relevant requirement slice used by planning and prompts."""
    normalized = path.casefold()
    path_words = set(re.findall(r"[a-z0-9]+", normalized))
    selected: list[str] = []
    for category in _MANIFEST_ID_CATEGORIES:
        for item in manifest.get(category, []):
            wording = str(item.get("name") or item.get("verbatim") or item.get("description") or "")
            tokens = {
                token for token in re.findall(r"[a-z0-9]+", wording.casefold())
                if len(token) >= 4 and token not in _MAPPING_STOP_WORDS
            }
            lexical = bool(tokens & path_words)
            routed = (
                category in {"inputs", "outputs", "use_cases"}
                and any(value in normalized for value in ("controller", "endpoint", "service", "component", "page", "openapi", "test"))
            ) or (
                category == "entities" and any(value in normalized for value in ("domain", "model", "entity", "schema", "migration", "repository", "database"))
            ) or (
                category == "roles_permissions" and any(value in normalized for value in ("auth", "security", "policy", "permission", "test"))
            ) or (
                category == "integrations" and any(value in normalized for value in ("config", "infrastructure", "docker", "k8s", "kubernetes", "bicep", "terraform", "pipeline", "workflow"))
            ) or (
                category == "non_functional_requirements" and any(value in normalized for value in ("test", "config", "middleware", "health", "telemetry", "docker", "infrastructure", "pipeline", "readme"))
            ) or (category == "deliverables" and lexical)
            if lexical or routed:
                selected.append(str(item["id"]))
    return selected


def _manifest_prompt_view(manifest: Mapping[str, Any], path: str | None) -> dict[str, Any]:
    """Build a compact prompt view while the immutable artifact retains verbatim input."""
    selected = set(_requirement_ids_for_path(path, manifest)) if path else None
    view = {
        key: value for key, value in manifest.items()
        if key != "source" and key not in _MANIFEST_ID_CATEGORIES
    }
    source = dict(manifest.get("source") or {})
    request = str(source.pop("request_verbatim", ""))
    instructions = str(source.pop("run_instructions_verbatim", ""))
    source.update({
        "request_sha256": hashlib.sha256(request.encode("utf-8")).hexdigest(),
        "request_characters": len(request),
        "run_instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
        "run_instructions_characters": len(instructions),
        "verbatim_source": "retained in the frozen on-disk manifest",
    })
    view["source"] = source
    for category in _MANIFEST_ID_CATEGORIES:
        items = [dict(item) for item in manifest.get(category, [])]
        view[category] = items if selected is None else [item for item in items if item.get("id") in selected]
    view["full_manifest_checksum"] = manifest.get("checksum")
    view["prompt_scope"] = path or "system"
    return view


def parse_generation_response(text: str, rel_path: str) -> str:
    """Extract raw file content and retain the model's machine-readable notes."""
    stripped = (text or "").strip()
    split_match = re.fullmatch(r"(?s)<split_request>\s*(\{.*\})\s*</split_request>", stripped)
    if split_match:
        payload = json.loads(split_match.group(1))
        files = tuple(payload.get("files") or ())
        if not files or any(not item.get("path") or not item.get("purpose") for item in files):
            raise ValueError(f"Invalid split request for {rel_path}")
        raise StructuredSplitRequest(rel_path, files, str(payload.get("reason") or "file exceeds safe generation size"))
    file_match = re.match(
        r"(?s)\s*<generated_file>\s*\n?(.*?)\n?</generated_file>", stripped,
    )
    if not file_match:
        with _NOTES_LOCK:
            _FILE_NOTES[rel_path] = {"format_valid": False, "error": "missing governed response envelope"}
        return text
    notes_match = re.search(
        r"(?s)</generated_file>\s*<generation_notes>\s*(.*?)\s*</generation_notes>\s*$",
        stripped,
    )
    if not notes_match:
        notes = {"format_valid": False, "error": "missing governed generation notes"}
        with _NOTES_LOCK:
            _FILE_NOTES[rel_path] = notes
        return file_match.group(1).rstrip() + "\n"
    try:
        notes = json.loads(notes_match.group(1))
    except (json.JSONDecodeError, TypeError) as exc:
        # The source file is still independently recoverable. Never let a
        # malformed accountability block become executable Java/TypeScript;
        # governed callers will reject the invalid notes and request repair.
        notes = {"format_valid": False, "error": f"invalid generation notes JSON: {exc.msg}"}
        with _NOTES_LOCK:
            _FILE_NOTES[rel_path] = notes
        return file_match.group(1).rstrip() + "\n"
    required = {
        "applied_rule_ids", "not_applicable_rules", "conflicts_resolved", "assumptions",
        "manifest_ids_covered", "manifest_coverage", "self_check",
    }
    missing = sorted(required - set(notes))
    if missing:
        notes["format_valid"] = False
        notes["error"] = "missing note keys: " + ", ".join(missing)
    else:
        list_fields = (
            "applied_rule_ids", "not_applicable_rules", "conflicts_resolved", "assumptions",
            "manifest_ids_covered", "manifest_coverage",
        )
        schema_errors = [
            f"{field} must be an array"
            for field in list_fields if not isinstance(notes.get(field), list)
        ]
        coverage = notes.get("manifest_coverage")
        if isinstance(coverage, list):
            for index, claim in enumerate(coverage):
                if not isinstance(claim, Mapping):
                    schema_errors.append(f"manifest_coverage[{index}] must be an object")
                    continue
                if not isinstance(claim.get("id"), str) or not claim.get("id"):
                    schema_errors.append(f"manifest_coverage[{index}].id must be a non-empty string")
                if not isinstance(claim.get("actions"), list) or not all(
                    isinstance(action, str) for action in (claim.get("actions") or [])
                ):
                    schema_errors.append(f"manifest_coverage[{index}].actions must be a string array")
        for field in ("applied_rule_ids", "manifest_ids_covered"):
            value = notes.get(field)
            if isinstance(value, list) and not all(isinstance(item, str) for item in value):
                schema_errors.append(f"{field} must be a string array")
        not_applicable = notes.get("not_applicable_rules")
        if isinstance(not_applicable, list) and not all(
            isinstance(item, Mapping) for item in not_applicable
        ):
            schema_errors.append("not_applicable_rules must contain objects")
        if not isinstance(notes.get("self_check"), Mapping):
            schema_errors.append("self_check must be an object")
        if schema_errors:
            # Never retain malformed nested collections: the audit deliberately
            # inspects invalid notes to report gaps, and raw strings here used
            # to crash that inspection before it could fail cleanly.
            notes = {
                "format_valid": False,
                "error": "invalid generation notes schema: " + "; ".join(schema_errors[:8]),
                "applied_rule_ids": [],
                "not_applicable_rules": [],
                "conflicts_resolved": [],
                "assumptions": [],
                "manifest_ids_covered": [],
                "manifest_coverage": [],
                "self_check": {},
            }
            with _NOTES_LOCK:
                _FILE_NOTES[rel_path] = notes
            return file_match.group(1).rstrip() + "\n"
        # Models commonly describe CI evidence with operational verbs.  Store
        # the canonical governance vocabulary so equivalent claims do not fail
        # only at the final gate after an otherwise complete multi-hour run.
        action_aliases = {
            "build": "implement", "deploy": "configure", "push": "transmit",
            "scan": "validate", "document": "produce",
        }
        for claim in coverage:
            claim["actions"] = list(dict.fromkeys(
                action_aliases.get(str(action).casefold(), str(action).casefold())
                for action in (claim.get("actions") or [])
            ))
        notes["format_valid"] = True
    with _NOTES_LOCK:
        _FILE_NOTES[rel_path] = notes
    return file_match.group(1).rstrip() + "\n"


def validate_split_files(request: StructuredSplitRequest, *, limit: int = 8) -> list[dict[str, str]]:
    """Validate a model split plan before any generated path is written."""
    if len(request.files) > limit:
        raise ValueError(f"Split request for {request.source_path} exceeds the {limit}-file limit")
    validated: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in request.files:
        path = str(raw.get("path") or "").replace("\\", "/").strip("/")
        parts = PurePosixPath(path).parts
        if not path or PurePosixPath(path).is_absolute() or ".." in parts:
            raise ValueError(f"Split request contains unsafe path: {path!r}")
        key = path.casefold()
        if key in seen:
            raise ValueError(f"Split request contains duplicate path: {path}")
        if PurePosixPath(path).suffix.casefold() not in _SOURCE_EXTENSIONS:
            raise ValueError(f"Split request contains unsupported source path: {path}")
        purpose = str(raw.get("purpose") or "").strip()
        if not purpose:
            raise ValueError(f"Split request lacks a purpose for {path}")
        seen.add(key)
        validated.append({"path": path, "purpose": purpose})
    return validated


def generation_notes_snapshot(prefix: str = "") -> dict[str, dict[str, Any]]:
    with _NOTES_LOCK:
        return {path: dict(value) for path, value in _FILE_NOTES.items() if not prefix or path.startswith(prefix)}


def register_generation_notes(rel_path: str, notes: Mapping[str, Any]) -> None:
    """Register accountability for deterministic, compiler-proven generators."""
    required = {
        "applied_rule_ids", "not_applicable_rules", "conflicts_resolved", "assumptions",
        "manifest_ids_covered", "manifest_coverage", "self_check",
    }
    missing = sorted(required - set(notes))
    if missing:
        raise ValueError("Deterministic generation notes lack: " + ", ".join(missing))
    stored = dict(notes)
    stored["format_valid"] = True
    stored["generator"] = "deterministic-compiler-proven"
    with _NOTES_LOCK:
        _FILE_NOTES[rel_path] = stored


def restore_valid_generation_notes(rel_path: str, notes: Mapping[str, Any]) -> None:
    """Restore a previously validated note snapshot after a failed repair.

    A targeted notes-only model call must be monotonic: malformed replacement
    JSON cannot erase valid, source-proven accountability that was already
    registered for the file.
    """
    if not notes.get("format_valid"):
        raise ValueError("Only valid generation notes can be restored")
    with _NOTES_LOCK:
        _FILE_NOTES[rel_path] = dict(notes)


def merge_verified_generation_actions(
    rel_path: str, additions: Sequence[Mapping[str, Any]],
) -> None:
    """Merge actions proven by direct source constructs into valid model notes.

    This is intentionally narrow: callers may supply only actions established
    by deterministic source inspection (for example logger, return, print, or
    explicit error constructs). It cannot create notes or rescue malformed
    notes, so accountability remains fail-closed.
    """
    with _NOTES_LOCK:
        current = _FILE_NOTES.get(rel_path)
        if not current or not current.get("format_valid"):
            return
        notes = dict(current)
        coverage = [dict(claim) for claim in notes.get("manifest_coverage") or []]
        by_id = {str(claim.get("id")): claim for claim in coverage if claim.get("id")}
        verified = []
        for addition in additions:
            item_id = str(addition.get("id") or "")
            actions = [str(action).casefold() for action in addition.get("actions") or []]
            if not item_id or not actions:
                continue
            claim = by_id.get(item_id)
            if claim is None:
                claim = {"id": item_id, "actions": []}
                coverage.append(claim)
                by_id[item_id] = claim
            claim["actions"] = list(dict.fromkeys([*(claim.get("actions") or []), *actions]))
            verified.append({"id": item_id, "actions": actions})
        if not verified:
            return
        notes["manifest_coverage"] = coverage
        notes["manifest_ids_covered"] = list(dict.fromkeys([
            *(notes.get("manifest_ids_covered") or []),
            *(item["id"] for item in verified),
        ]))
        notes["source_verified_action_additions"] = verified
        _FILE_NOTES[rel_path] = notes


def clear_generation_notes(prefix: str = "") -> None:
    with _NOTES_LOCK:
        for path in list(_FILE_NOTES):
            if not prefix or path.startswith(prefix):
                _FILE_NOTES.pop(path, None)


def validate_machine_rules(
    output: Mapping[str, str],
    guideline_set: Mapping[str, Any],
    language: str,
) -> list[str]:
    diagnostics: list[str] = []
    for path, content in output.items():
        if not isinstance(content, str):
            continue
        normalized = path.replace("\\", "/").casefold()
        # Governance reports and generator provenance are evidence about the
        # run, not generated application code. Applying user source rules to
        # those files is self-referential and can make a rule ban its own text.
        if (
            "/governance/" in f"/{normalized.lstrip('/')}"
            or "/.strat-aqorynth/" in f"/{normalized.lstrip('/')}"
            or normalized.endswith("_generation_audit.md")
        ):
            continue
        suffix = PurePosixPath(path).suffix.casefold()
        basename = PurePosixPath(path).name.casefold()
        is_source = suffix in _SOURCE_EXTENSIONS
        is_machine_content = (
            is_source or suffix in _CONFIG_EXTENSIONS or basename in {
                value.casefold() for value in _CONFIG_FILENAMES
            }
        )
        if is_source:
            placeholder = _first_placeholder(content, path)
            if placeholder:
                diagnostics.append(f"{path}: incomplete placeholder construct {placeholder.group(0)!r}")
        for rule in applicable_rules(guideline_set.get("rules") or [], path, language):
            check = rule.get("machine_check") or {}
            if check.get("kind") == "banned_tokens":
                if not is_machine_content:
                    continue
                diagnostics.extend(
                    f"{path}: {rule['id']} bans {token!r}"
                    for token in check.get("values") or []
                    if token and token in content
                )
            elif check.get("kind") == "required_header" and not content.startswith(str(check.get("value") or "")):
                diagnostics.append(f"{path}: {rule['id']} required header is missing")
            elif check.get("kind") == "path_pattern":
                try:
                    if not re.search(str(check.get("value")), path):
                        diagnostics.append(f"{path}: {rule['id']} naming pattern does not match")
                except re.error:
                    diagnostics.append(f"{rule['id']}: invalid naming regex in supplied guideline")
    return diagnostics


def build_file_plan(
    paths: Sequence[str], manifest: Mapping[str, Any], guideline_set: Mapping[str, Any], language: str,
) -> list[dict[str, Any]]:
    path_set = set(paths)
    plan: list[dict[str, Any]] = []
    for index, path in enumerate(paths, 1):
        stem = str(PurePosixPath(path).with_suffix(""))
        candidates = [
            f"{stem}Test{PurePosixPath(path).suffix}",
            f"{stem}.test{PurePosixPath(path).suffix}",
            f"{stem}.spec{PurePosixPath(path).suffix}",
        ]
        paired = next((candidate for candidate in candidates if candidate in path_set), None)
        plan.append({
            "order": index, "path": path, "paired_test": paired,
            "requirement_ids": _requirement_ids_for_path(path, manifest),
            "rule_ids": [rule["id"] for rule in applicable_rules(guideline_set.get("rules") or [], path, language)],
        })
    return plan


def _supported_trace_actions(
    path: str,
    category: str,
    actions: Iterable[str],
    *,
    ui_source: bool = False,
) -> list[str]:
    """Return evidence actions that the artifact type can truthfully perform."""
    lower = path.casefold().replace("\\", "/")
    suffix = PurePosixPath(lower).suffix
    is_source = suffix in _SOURCE_EXTENSIONS
    is_test = any(token in lower for token in (
        "/test/", "/tests/", ".spec.", ".test.", "expected-output", "golden",
    ))
    is_ui = (
        ui_source
        or suffix in {".html", ".tsx", ".jsx", ".vue"}
        or "/frontend/" in lower
    )
    is_persistence = suffix in {".sql", ".cob", ".cbl"} or any(token in lower for token in (
        "/repository/", "/repositories/", "/entity/", "/entities/",
        "/domain/", "/infrastructure/", "dbcontext", "repository.",
    ))
    is_authorization = is_source and any(token in lower for token in (
        "/auth/", "/security/", "/policies/", "/authorization/",
        "authorize", "permission", "controller",
    ))
    is_configuration = suffix in {
        ".json", ".yaml", ".yml", ".toml", ".xml", ".tf", ".hcl", ".bicep", ".bicepparam",
    }
    non_test_source = is_source and not is_test
    allowed_by_category = {
        # Browser components capture and validate user input, but durable audit
        # evidence belongs at the trusted server boundary. Requiring an Angular
        # or React component to log credentials or authentication attempts is
        # both architecturally false and a security risk. Backend source still
        # has to provide the audit action.
        "inputs": (
            ({"capture", "validate"} if is_ui else {"capture", "validate", "audit"})
            if non_test_source or is_ui else set()
        ) | ({"test"} if is_test else set()),
        "outputs": ({"produce", "render", "error-state", "audit"} if non_test_source or is_ui else set()) | ({"test"} if is_test else set()),
        "entities": ({"persist", "implement", "audit"} if is_persistence and not is_test else set()) | ({"test"} if is_test else set()),
        "use_cases": ({"implement", "audit"} if non_test_source else set()) | ({"test"} if is_test else set()),
        "roles_permissions": ({"authorize", "implement", "audit"} if is_authorization and not is_test else set()) | ({"test"} if is_test else set()),
        "integrations": ({"configure"} if is_configuration else set()) | ({"implement", "audit"} if non_test_source else set()) | ({"test"} if is_test else set()),
        "non_functional_requirements": ({"configure"} if is_configuration else set()) | ({"implement", "audit"} if non_test_source else set()) | ({"test"} if is_test else set()),
        "deliverables": ({"produce", "audit"} if not is_test else set()) | ({"test"} if is_test else set()),
    }
    permitted = allowed_by_category.get(category, set())
    return [action for action in actions if action in permitted]


def register_deterministic_plan_notes(
    project_name: str,
    deterministic_paths: Iterable[str],
    file_plan: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    guideline_set: Mapping[str, Any],
    language: str,
) -> None:
    """Attach auditable, path-bounded evidence to generator-owned scaffold files."""
    categories = {
        item["id"]: category
        for category in _MANIFEST_ID_CATEGORIES
        for item in manifest.get(category, [])
    }
    plan_by_path = {str(entry.get("path") or ""): entry for entry in file_plan}
    rules = guideline_set.get("rules") or []
    existing_notes = generation_notes_snapshot(f"{project_name}/")
    for relative in deterministic_paths:
        entry = plan_by_path.get(relative)
        if not entry:
            continue
        coverage = []
        for item_id in entry.get("requirement_ids") or []:
            category = categories.get(item_id)
            if not category:
                continue
            candidate_actions = set(_TRACE_REQUIREMENTS[category][1]) | {"audit", "configure", "produce"}
            actions = _supported_trace_actions(relative, category, sorted(candidate_actions))
            if actions:
                coverage.append({"id": item_id, "actions": actions})
        full_path = f"{project_name}/{relative}"
        if existing_notes.get(full_path, {}).get("format_valid"):
            continue
        register_generation_notes(full_path, {
            "applied_rule_ids": [rule["id"] for rule in applicable_rules(rules, relative, language)],
            "not_applicable_rules": [],
            "conflicts_resolved": guideline_set.get("conflicts") or [],
            "assumptions": manifest.get("assumptions") or [],
            "manifest_ids_covered": [claim["id"] for claim in coverage],
            "manifest_coverage": coverage,
            "self_check": {
                "complete": True,
                "deterministic_generator": True,
                "contracts_match": True,
            },
        })


def build_audit_artifacts(
    project_name: str,
    manifest: Mapping[str, Any],
    guideline_set: Mapping[str, Any],
    standards: Sequence[Mapping[str, Any]],
    file_plan: Sequence[Mapping[str, Any]],
    output: Mapping[str, str],
    language: str,
    single_artifact: bool = False,
    unvalidated_paths: Iterable[str] = (),
) -> tuple[dict[str, str], list[str]]:
    """Create Phase-E reports and return enforcement diagnostics.

    ``single_artifact`` is used only where one non-test implementation file is
    the complete deliverable (including an explicitly contracted one-script
    SQL project). Required trace actions
    that such a file cannot truthfully perform (for example ``test`` evidence,
    or ``persist`` from a non-persistence file) are recorded per requirement as
    not applicable instead of being impossible-to-satisfy failures. Every
    remaining action must still be claimed by the model's own notes.
    """
    prefix = f"{project_name}/"
    unvalidated = {
        str(path).replace("\\", "/").lstrip("/") for path in unvalidated_paths
    }
    notes = generation_notes_snapshot(prefix)
    requested_evaluation_mode = "single_artifact" if single_artifact else "multi_file"
    ancillary_names = {"readme.md", "_generation_audit.md"}
    planned_implementation_paths = [
        str(entry.get("path") or "").replace("\\", "/").lstrip("/")
        for entry in file_plan
        if str(entry.get("path") or "").replace("\\", "/").lstrip("/")
        and PurePosixPath(str(entry.get("path") or "")).name.casefold() not in ancillary_names
        and "/governance/" not in f"/{str(entry.get('path') or '').casefold().strip('/')}"
        and "/.strat-aqorynth/" not in f"/{str(entry.get('path') or '').casefold().strip('/')}"
    ]
    evaluation_mode_diagnostics: list[str] = []
    if single_artifact and len(planned_implementation_paths) != 1:
        evaluation_mode_diagnostics.append(
            "Governance evaluation mode mismatch: single-artifact rules were requested for "
            f"{len(planned_implementation_paths)} implementation files; multi-file rules were enforced"
        )
        single_artifact = False
    applied_evaluation_mode = "single_artifact" if single_artifact else "multi_file"
    files = [
        path for path in output
        if path.startswith(prefix)
        and "/governance/" not in path
        and "/.strat-aqorynth/" not in path
        and not path.casefold().endswith("_generation_audit.md")
    ]
    diagnostics = [
        *evaluation_mode_diagnostics,
        *validate_machine_rules(output, guideline_set, language),
    ]
    rule_rows: list[dict[str, Any]] = []
    for rule in guideline_set.get("rules") or []:
        applicable = [path for path in files if rule["id"] in [item["id"] for item in applicable_rules([rule], path, language)]]
        applied = [path for path, note in notes.items() if rule["id"] in (note.get("applied_rule_ids") or [])]
        na = [
            {"path": path, "reason": item.get("reason")}
            for path, note in notes.items() for item in (note.get("not_applicable_rules") or [])
            if item.get("id") == rule["id"] and item.get("reason")
        ]
        status = "applied" if applied else "not_applicable" if na or not applicable else "unaccounted"
        if status == "unaccounted":
            diagnostics.append(f"{rule['id']}: applicable guideline was neither applied nor justified N/A")
        rule_rows.append({
            "id": rule["id"], "verbatim": rule["verbatim"], "precedence": rule["precedence"],
            "applicable_files": applicable, "applied_files": applied,
            "not_applicable": na or ([{"path": None, "reason": "No generated file matched the rule scope/language"}] if not applicable else []),
            "status": status,
        })
    trace_rows: list[dict[str, Any]] = []
    allowed_actions = {
        "capture", "validate", "persist", "audit", "produce", "render", "transmit",
        "test", "error-state", "implement", "authorize", "configure",
    }
    known_ids = {
        item["id"]
        for category in _MANIFEST_ID_CATEGORIES
        for item in manifest.get(category, [])
    }
    for path, note in notes.items():
        for claim in note.get("manifest_coverage") or []:
            claim_id = claim.get("id")
            actions = set(claim.get("actions") or [])
            if claim_id not in known_ids:
                diagnostics.append(f"{path}: unknown manifest coverage ID {claim_id!r}")
            invalid = sorted(actions - allowed_actions)
            if invalid:
                diagnostics.append(f"{path}: invalid manifest actions for {claim_id}: {', '.join(invalid)}")
    planned_paths = {str(entry.get("path") or "").replace("\\", "/").lstrip("/") for entry in file_plan}
    actual_paths = {
        path.removeprefix(prefix).replace("\\", "/") for path in files
        if "/.strat-aqorynth/" not in path and not path.endswith("_GENERATION_AUDIT.md")
    }
    missing_planned = sorted(planned_paths - actual_paths)
    unplanned_emitted = sorted(actual_paths - planned_paths)
    diagnostics.extend(f"Planned file was not emitted: {path}" for path in missing_planned)
    diagnostics.extend(f"Emitted file was not in the frozen file plan: {path}" for path in unplanned_emitted)

    # When the request supplies no NFR section, the manifest injects project
    # defaults (authorization, telemetry, retries, build/tests/deployment docs)
    # and records that as an assumption. For a single-file deliverable those
    # defaults are not the user's requirements and mostly cannot be realized
    # by one file, so they are reported as advisory rather than failures.
    # NFRs the user actually wrote are still enforced.
    defaulted_nfrs = single_artifact and any(
        "non-functional defaults apply" in str(assumption.get("text") or "")
        for assumption in manifest.get("assumptions") or []
    )
    implicit_input_contract = single_artifact and any(
        "No discrete input-field list was supplied" in str(assumption.get("text") or "")
        for assumption in manifest.get("assumptions") or []
    )
    implicit_output_contract = single_artifact and any(
        "No discrete output list was supplied" in str(assumption.get("text") or "")
        for assumption in manifest.get("assumptions") or []
    )
    single_ui_source = single_artifact and any(
        any(token in str(pack.get("id") or "").casefold() for token in ("angular", "react", "vue"))
        for pack in standards
    )
    explicit_single_input_audit = bool(re.search(
        r"(?i)\b(?:audit|logging|logger|log\s+(?:the\s+)?(?:operation|outcome|result)|"
        r"record\s+(?:the\s+)?(?:operation|outcome|validation|result))\b",
        str((manifest.get("source") or {}).get("request_verbatim") or ""),
    ))
    for category in _MANIFEST_ID_CATEGORIES:
        for item in manifest.get(category, []):
            planned = [entry["path"] for entry in file_plan if item["id"] in entry.get("requirement_ids", [])]
            planned_full_paths = {f"{prefix}{path}" for path in planned}
            validated_planned_paths = {
                path for path in planned_full_paths
                if path.replace("\\", "/").lstrip("/") not in unvalidated
            }
            claims = [
                {
                    "path": path,
                    "actions": _supported_trace_actions(
                        path, category, claim.get("actions") or [], ui_source=single_ui_source,
                    ),
                }
                for path, note in notes.items() for claim in (note.get("manifest_coverage") or [])
                if claim.get("id") == item["id"] and path in output and path in validated_planned_paths
            ]
            claims = [claim for claim in claims if claim["actions"]]
            actions = {action for claim in claims for action in claim["actions"]}
            kind, required_actions = _TRACE_REQUIREMENTS[category]
            not_applicable_actions: list[str] = []
            implicit_static_sql_input = False
            implicit_static_sql_output = False
            if single_artifact and planned:
                performable = {
                    action
                    for path in planned
                    for action in _supported_trace_actions(
                        path, category, sorted(required_actions), ui_source=single_ui_source,
                    )
                }
                if category == "inputs" and not explicit_single_input_audit:
                    performable.discard("audit")
                if category == "inputs" and implicit_input_contract:
                    planned_sql = [path for path in planned if PurePosixPath(path).suffix.casefold() == ".sql"]
                    runtime_input = any(re.search(
                        r"(?is)\b(?:procedure|function)\s+[\w$#]+\s*\(\s*[^)]"
                        r"|(?<!:):[A-Za-z][A-Za-z0-9_$#]*\b|\$[1-9]\d*\b|&[A-Za-z]\w*",
                        str(output.get(f"{prefix}{path}") or ""),
                    ) for path in planned_sql)
                    implicit_static_sql_input = bool(
                        planned_sql and len(planned_sql) == len(planned) and not runtime_input
                    )
                    if implicit_static_sql_input:
                        performable.difference_update({"capture", "validate", "audit"})
                if category == "outputs" and implicit_output_contract:
                    planned_sql = [path for path in planned if PurePosixPath(path).suffix.casefold() == ".sql"]
                    # A schema/migration/view script is itself the requested
                    # artifact. It has no runtime response channel in which it
                    # could render a result or expose an error state. The
                    # frozen manifest's generic OUT-001 is an explicit
                    # assumption in this case, not a user-supplied runtime
                    # output requirement. Executable SQL routines remain
                    # governed: OUT/RETURN values, SELECT INTO, DBMS_OUTPUT,
                    # bind assignments, and RETURNING INTO are all runtime
                    # output evidence and therefore keep the ordinary output
                    # actions mandatory.
                    runtime_output = any(re.search(
                        r"(?is)\b(?:procedure|function)\s+[\w$#]+\s*\([^)]*\b(?:out|in\s+out)\b"
                        r"|\bfunction\s+[\w$#]+(?:\s*\([^)]*\))?\s+return\s+"
                        r"|\bselect\b[\s\S]{0,1000}?\binto\b"
                        r"|\breturning\b[\s\S]{0,500}?\binto\b"
                        r"|\bdbms_output\.put_line\s*\("
                        r"|(?<!:):[A-Za-z][A-Za-z0-9_$#]*\s*:=",
                        str(output.get(f"{prefix}{path}") or ""),
                    ) for path in planned_sql)
                    implicit_static_sql_output = bool(
                        planned_sql and len(planned_sql) == len(planned) and not runtime_output
                    )
                    if implicit_static_sql_output:
                        performable.difference_update({"produce", "render", "error-state"})
                not_applicable_actions = sorted(required_actions - performable)
                required_actions = required_actions & performable
            advisory = defaulted_nfrs and category == "non_functional_requirements"
            missing_actions = sorted(required_actions - actions)
            traceability_deferred = bool(planned_full_paths and not validated_planned_paths)
            if not planned and not single_artifact:
                diagnostics.append(
                    f"{item['id']}: requirement has no planned file under multi-file governance"
                )
            if missing_actions and not advisory and not traceability_deferred:
                diagnostics.append(f"{item['id']}: no traceability evidence")
            row = {
                "id": item["id"], "kind": kind,
                "name": item.get("name") or item.get("verbatim"),
                "claims": claims, "planned_files": planned, "required_actions": sorted(required_actions),
                "missing_actions": missing_actions,
                "planning_status": "planned" if planned else "unplanned",
                "evaluation_mode": applied_evaluation_mode,
            }
            if traceability_deferred:
                row["traceability_deferred"] = True
                row["traceability_deferred_reason"] = (
                    "All planned source files for this requirement failed strict source validation; "
                    "traceability will be evaluated only after source validation passes"
                )
            if single_artifact:
                row["advisory"] = advisory
                if advisory:
                    row["advisory_reason"] = (
                        "Default project NFR injected because the request had no NFR section; "
                        "reported but not enforced for a single-file deliverable"
                    )
                row["not_applicable_actions"] = not_applicable_actions
                row["not_applicable_reason"] = (
                    "Implicit design-time request has no runtime input interface in this static SQL artifact"
                    if implicit_static_sql_input else
                    "Implicit design-time result is the emitted static SQL artifact, not a runtime response interface"
                    if implicit_static_sql_output else
                    "Single-file deliverable: the generated artifact type cannot perform these actions"
                    if not_applicable_actions else ""
                )
            trace_rows.append(row)
    invalid_notes = [path for path, note in notes.items() if not note.get("format_valid")]
    diagnostics.extend(f"{path}: generation accountability notes are missing or invalid" for path in invalid_notes)
    audit = {
        "schema_version": "1.0.0", "project": project_name,
        "requirements_manifest_checksum": manifest["checksum"],
        "guideline_set_checksum": guideline_set["checksum"],
        "selected_standard_packs": [{"id": pack["id"], "version": pack["version"]} for pack in standards],
        "file_count": len(files), "file_notes_received": len(notes),
        "conflicts": guideline_set.get("conflicts") or [], "diagnostics": diagnostics,
        "evaluation_mode": {
            "requested": requested_evaluation_mode,
            "applied": applied_evaluation_mode,
            "implementation_files": planned_implementation_paths,
            "mismatch": bool(evaluation_mode_diagnostics),
        },
        "file_plan_parity": {
            "passed": not missing_planned and not unplanned_emitted,
            "missing_planned_files": missing_planned,
            "unplanned_emitted_files": unplanned_emitted,
        },
        "generation_gate_passed": not diagnostics,
        "production_ready": False,
        "production_readiness": "not_assessed_by_code_generation",
    }
    artifacts = {
        f"{prefix}governance/requirements-manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        f"{prefix}governance/guidelines.json": json.dumps(guideline_set, ensure_ascii=False, indent=2) + "\n",
        f"{prefix}governance/language-standards.json": json.dumps(list(standards), ensure_ascii=False, indent=2) + "\n",
        f"{prefix}governance/file-plan.json": json.dumps(list(file_plan), ensure_ascii=False, indent=2) + "\n",
        f"{prefix}governance/generation-audit.json": json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
        f"{prefix}governance/guideline-compliance.json": json.dumps(rule_rows, ensure_ascii=False, indent=2) + "\n",
        f"{prefix}governance/input-output-traceability.json": json.dumps(trace_rows, ensure_ascii=False, indent=2) + "\n",
    }
    return artifacts, diagnostics


def validate_language_pack_coverage(supported_languages: Iterable[str]) -> list[str]:
    return [
        language
        for language in sorted({value.casefold() for value in supported_languages if value})
        if not (_PACK_ROOT / f"{language}.json").is_file()
    ]


def load_language_acceptance_policy(language: str) -> dict[str, Any]:
    """Load the editable build-manifest and runnable-test contract for a language."""
    document = json.loads(_ACCEPTANCE_POLICY_PATH.read_text(encoding="utf-8"))
    policy = (document.get("languages") or {}).get((language or "").casefold())
    if not isinstance(policy, dict):
        raise ValueError(f"No language acceptance policy is registered for {language!r}")
    return {
        "schema_version": document.get("schema_version"),
        "language": (language or "").casefold(),
        "manifests": list(policy.get("manifests") or []),
        "tests": list(policy.get("tests") or []),
    }


def validate_language_acceptance_coverage(supported_languages: Iterable[str]) -> list[str]:
    document = json.loads(_ACCEPTANCE_POLICY_PATH.read_text(encoding="utf-8"))
    registered = set(document.get("languages") or {})
    return sorted({value.casefold() for value in supported_languages if value} - registered)

