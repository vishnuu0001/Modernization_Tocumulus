"""Contract-derived, model-aware token planning for generated source files."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

END_SENTINEL = "<<<END_OF_FILE>>>"
SAFETY_FACTOR = 1.35
PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|FIXME|NotImplementedException|NotImplementedError|\.\.\.\s*rest of)\b",
    re.IGNORECASE,
)
_GENERIC_CONTRACT_IDENTIFIERS = {
    "API", "CRUD", "CSV", "DDL", "DML", "DTO", "HTML", "HTTP", "HTTPS",
    "JSON", "REST", "SOAP", "SQL", "UI", "URI", "URL", "XML",
}
MAX_CONTRACT_MEMBERS = 80

LINES_PER_UNIT = {
    "controller": {"base": 40, "endpoint": 28, "dependency": 3},
    "service": {"base": 30, "method": 25, "dependency": 3},
    "repository": {"base": 25, "method": 18, "dependency": 2},
    "dto": {"base": 10, "field": 2},
    "entity": {"base": 15, "field": 3},
    "component": {"base": 45, "method": 15, "field": 2},
    "test": {"base": 30, "case": 22},
    "config": {"base": 40},
    "default": {"base": 40, "method": 20, "field": 2},
}
TOKENS_PER_LINE = {
    "cs": 11.0, "ts": 10.0, "tsx": 10.0, "java": 11.5,
    "html": 9.0, "cbl": 9.5, "sql": 10.0, "default": 10.5,
}


@dataclass(frozen=True)
class FileContract:
    path: str
    kind: str
    members: list[dict] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)

    @property
    def ext(self) -> str:
        return self.path.rsplit(".", 1)[-1].lower() if "." in self.path else ""


@dataclass(frozen=True)
class Budget:
    max_new_tokens: int
    fits_single_shot: bool
    estimate: int
    available: int
    prompt_tokens: int
    context_window: int


class IncompleteArtifact(RuntimeError):
    def __init__(self, path: str, reasons: list[str]):
        super().__init__(f"{path}: " + "; ".join(reasons))
        self.path = path
        self.reasons = reasons


def _member_type(text: str) -> str:
    lowered = text.casefold()
    if any(word in lowered for word in ("endpoint", "route", "api")):
        return "endpoint"
    if any(word in lowered for word in ("test", "case", "scenario")):
        return "case"
    if any(word in lowered for word in ("field", "column", "property")):
        return "field"
    return "method"


def contract_from_prompt(path: str, prompt: str, language: str) -> FileContract:
    lowered = f"{path} {prompt}".casefold()
    kind = next((value for value in LINES_PER_UNIT if value != "default" and value in lowered), "default")
    members = []
    member_names: dict[str, int] = {}
    seen_signatures: set[str] = set()
    contract_items = list(re.finditer(
        r"(?m)^\s*(?P<prefix>\d+[.)]|[-*])\s+(?P<text>.+?)\s*$", prompt,
    ))
    structure_items = list(re.finditer(
        r"(?m)^\s*--\s*(?P<prefix>\d+[.)])\s+(?P<text>.+?)\s*$", prompt,
    ))
    numbered_items = [
        match for match in contract_items
        if match.group("prefix")[0].isdigit()
    ]
    # An explicit commented SCRIPT STRUCTURE is the strongest ordered file
    # contract. Otherwise numbered items normally define deliverables;
    # surrounding bullets are context and acceptance policy, not independent
    # generation calls.
    selected_items = (
        structure_items if len(structure_items) >= 3 else
        # Even one explicitly numbered deliverable is a stronger contract than
        # surrounding acceptance-policy bullets. Treating all bullets as
        # independent members multiplies expensive fallback calls and can turn
        # concerns such as logging/security into invalid source fragments.
        numbered_items if numbered_items else
        contract_items
    )
    for match in selected_items:
        text = match.group("text").strip()
        if len(text) < 4:
            continue
        # Project metadata can serialize the same custom-stack requirement
        # more than once (for example once as the target description and once
        # as run instructions). Repeated numbered blocks are not additional
        # deliverables. Treating them as new members doubles generation work
        # and can make a valid bounded plan time out.
        signature_key = re.sub(r"\s+", " ", text).strip().casefold()
        if signature_key in seen_signatures:
            continue
        seen_signatures.add(signature_key)
        name_match = next(
            (
                candidate for candidate in re.finditer(r"\b[A-Z][A-Z0-9_]{2,}\b", text)
                if candidate.group(0) not in _GENERIC_CONTRACT_IDENTIFIERS
            ),
            None,
        )
        if not name_match:
            name_match = re.search(
                r"(?i)\b(?:class|interface|function|procedure|view|method|endpoint)\s+"
                r"([A-Za-z_][A-Za-z0-9_]*)",
                text,
            )
        name = name_match.group(0) if name_match else f"requirement_{len(members) + 1}"
        if name_match and name_match.lastindex:
            name = name_match.group(1)
        key = name.casefold()
        member_names[key] = member_names.get(key, 0) + 1
        marker_name = name if member_names[key] == 1 else f"{name}_{member_names[key]}"
        members.append({
            "name": name, "marker_name": marker_name,
            "coverage_name": name if name_match else "",
            "type": _member_type(text), "signature": text,
        })
    if len(members) > MAX_CONTRACT_MEMBERS:
        raise ValueError(
            f"Single-file contract contains {len(members)} explicit members; "
            f"the bounded limit is {MAX_CONTRACT_MEMBERS}. Split the artifact into "
            "smaller requests or use project mode. No members were silently omitted."
        )
    dependencies = sorted(set(re.findall(
        r"\b(?:Spring|React|Angular|Oracle|PostgreSQL|SQL Server|Kafka|Redis|FastAPI|ASP\.NET)\b",
        prompt, re.IGNORECASE,
    )))
    return FileContract(path=path, kind=kind, members=members, dependencies=dependencies)


def estimate_output_tokens(contract: FileContract) -> int:
    weights = LINES_PER_UNIT.get(contract.kind, LINES_PER_UNIT["default"])
    lines = weights.get("base", 40)
    for member in contract.members:
        lines += weights.get(member.get("type", "method"), weights.get("method", 20))
    lines += weights.get("dependency", 0) * len(contract.dependencies)
    per_line = TOKENS_PER_LINE.get(contract.ext, TOKENS_PER_LINE["default"])
    return math.ceil(lines * per_line * SAFETY_FACTOR)


def plan_budget(model: str, messages, contract: FileContract, *, requested_context: int = 0,
                reserve: int = 512, minimum: int = 2048) -> Budget:
    from services.llm import count_tokens, model_context_window, model_max_output_cap

    prompt_tokens = count_tokens(messages)
    context_window = model_context_window(model, requested_context)
    available = min(model_max_output_cap(model), context_window - prompt_tokens - reserve)
    if available <= 0:
        raise RuntimeError(
            f"Prompt for {contract.path} exceeds the {context_window:,}-token context window "
            f"({prompt_tokens:,} prompt tokens); trim attached context before generating."
        )
    estimate = estimate_output_tokens(contract)
    wanted = max(minimum, math.ceil(estimate * 1.25))
    return Budget(
        max_new_tokens=min(wanted, available),
        fits_single_shot=estimate <= available,
        estimate=estimate,
        available=available,
        prompt_tokens=prompt_tokens,
        context_window=context_window,
    )


def growth_budgets(initial: int, available: int) -> list[int]:
    """Grow from the estimate to model capacity without a fixed 12K ceiling."""
    budgets = [max(256, min(initial, available))]
    while budgets[-1] < available:
        grown = max(budgets[-1] + 1024, math.ceil(budgets[-1] * 1.6 / 256) * 256)
        budgets.append(min(available, grown))
        if len(budgets) >= 6:
            if budgets[-1] != available:
                budgets.append(available)
            break
    return list(dict.fromkeys(budgets))


def sentinel_comment(language: str) -> str:
    language = language.casefold()
    return (
        f"-- {END_SENTINEL}" if language in {"sql", "haskell", "ada"} else
        f"      *> {END_SENTINEL}" if language == "cobol" else
        f"# {END_SENTINEL}" if language in {
            "python", "ruby", "shell", "bash", "elixir", "julia", "r",
        } else
        f"; {END_SENTINEL}" if language in {"clojure", "lisp"} else
        f"% {END_SENTINEL}" if language in {"erlang", "prolog"} else
        f"! {END_SENTINEL}" if language == "fortran" else
        f"<!-- {END_SENTINEL} -->" if language in {"svg", "xml", "html"} else
        f"// {END_SENTINEL}"
    )


def strip_sentinel(content: str) -> str:
    lines = [line for line in content.splitlines() if END_SENTINEL not in line]
    return "\n".join(lines).rstrip() + "\n"


def completeness_reasons(content: str, contract: FileContract) -> list[str]:
    reasons = []
    if END_SENTINEL not in content[-300:]:
        reasons.append("missing end sentinel (likely truncated)")
    if PLACEHOLDER_RE.search(content):
        reasons.append("placeholder or stub code present")
    lowered = content.casefold()
    for member in contract.members:
        name = str(member.get("coverage_name") or "").strip()
        if name and name.casefold() not in lowered:
            reasons.append(f"contract member not implemented: {name}")
    return reasons
