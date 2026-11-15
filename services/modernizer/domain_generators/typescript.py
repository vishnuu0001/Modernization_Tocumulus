# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: Modernization — services/modernizer/domain_generators (typescript.py)
# Date: 2026-02-26
# ---------------------------------------------------------------------------
from __future__ import annotations

import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)


def _framework_file_paths(base: str, domain: str, framework: str) -> tuple[str, str]:
    """Return framework-native paths instead of renaming React TSX to ``.ts``."""
    slug = re.sub(r"[^a-z0-9]+", "-", domain.casefold()).strip("-") or "feature"
    if framework == "Angular":
        angular_base = f"ModernizedApp/frontend/src/app/features/{slug}"
        return f"{angular_base}/{slug}.component.ts", f"{angular_base}/{slug}.api.ts"
    if framework == "Vue 3":
        vue_base = f"ModernizedApp/frontend/src/features/{slug}"
        return f"{vue_base}/{domain}Page.vue", f"{vue_base}/{domain}Api.ts"
    return f"{base}/{domain}Page.tsx", f"{base}/{domain}Service.ts"


def _source_grounding_rules(domain: str, source_sec: str) -> str:
    return (
        "\nSOURCE-GROUNDED MODERNIZATION RULES (mandatory):\n"
        f"- Preserve the observed {domain} behavior, route semantics, validation, and data shapes from SOURCE EVIDENCE.\n"
        "- Do not invent business fields, workflows, database tables, or endpoints merely to make a generic CRUD demo.\n"
        "- When source evidence does not establish a field or operation, omit it or isolate the assumption explicitly; never silently fabricate it.\n"
        "- Keep UI and API client routes, query parameters, request/response DTOs, nullability, and errors mutually consistent.\n"
        "- Prefer cohesive functions, explicit types, dependency injection, cancellation/unsubscription, accessible controls, and testable state transitions.\n"
        f"{source_sec}"
    )


def _ui_component_prompt(
    framework: str, domain: str, target: dict, context: str, prod_rules: str,
    source_sec: str, guide_sec: str, domain_tables: list[str],
) -> str:
    grounding = _source_grounding_rules(domain, source_sec)
    if framework == "Angular":
        angular_version = target.get("frontend_tech", "Angular 20")
        return (
            f"{context}\n{prod_rules}{guide_sec}{grounding}\n\n"
            f"Generate one complete, production-quality {angular_version} standalone component for the {domain} feature.\n"
            "- Use @Component({standalone: true}), ChangeDetectionStrategy.OnPush, inject(), signals/computed, and typed reactive forms.\n"
            "- Prefer inject(TOKEN) field initializers; do not use constructor parameter decorators such as @Inject(DOCUMENT).\n"
            "- Import only real Angular APIs and the feature API service from the sibling .api file; never use React hooks, JSX, Vue APIs, or NgModules.\n"
            "- Provide a complete inline template with semantic HTML, labels, keyboard-accessible actions, and source-supported loading/empty/error/success states.\n"
            "- Keep network calls in the injected API service. The component owns view state, form validation, confirmation, and user feedback without duplicating transport logic.\n"
            "- Clean up subscriptions with takeUntilDestroyed or an equivalent Angular lifecycle-safe pattern.\n"
            "- Use page and pageSize query semantics consistently with the generated .NET endpoint contract when pagination is source-supported.\n"
            f"- Source tables supported by evidence: {', '.join(domain_tables) or '(none detected)'}.\n"
            "Return only the complete .component.ts source. No Markdown or explanatory prose."
        )
    if framework == "Vue 3":
        return (
            f"{context}\n{prod_rules}{guide_sec}{grounding}\n\n"
            f"Generate one complete Vue 3 Single File Component for the {domain} feature using <script setup lang=\"ts\">, the Composition API, typed refs/computed state, accessible template markup, and scoped styles. "
            "Keep transport logic in the sibling API module. Never use React hooks or Angular decorators. Return only the complete .vue file."
        )
    return (
        f"{context}\n{prod_rules}{guide_sec}{grounding}\n\n"
        f"Generate one complete production-quality React TypeScript component named {domain}Page.\n"
        "- Use typed props/state, function components, useEffect cleanup, useCallback/useMemo only where they prevent real work, and the sibling API module for transport.\n"
        "- Implement accessible loading, empty, error, success, validation, and pagination states supported by the source contract.\n"
        "- Keep the page concise and bounded (prefer under 350 lines): use data-driven rendering and small local view helpers instead of repeated markup, and never embed mock/demo datasets.\n"
        "- Every const-assigned function and typed React handler must include its implementation arrow, including after a variable type annotation: `const submit: FormEventHandler<HTMLFormElement> = async (event) => {}`.\n"
        "- Every React hook callback must also include its arrow: use `useEffect(() => {})`, `useMemo(() => value, [])`, and `useCallback(() => {}, [])`; never write `useEffect(() {})`.\n"
        f"- Declare {domain}Page exactly once and export it exactly once; finish with `export default {domain}Page;` only when it was not already declared with `export default`. Never combine a stray closing brace with an export statement.\n"
        "- Never use Angular decorators, Vue APIs, fabricated fields, or endpoints absent from source evidence.\n"
        "Return only the complete .tsx source. No Markdown or explanatory prose."
    )


def _api_service_prompt(framework: str, domain: str, context: str, prod_rules: str, source_sec: str) -> str:
    grounding = _source_grounding_rules(domain, source_sec)
    if framework == "Angular":
        return (
            f"{context}\n{prod_rules}{grounding}\n\n"
            f"Generate one complete Angular API service module for the {domain} feature.\n"
            "- Use @Injectable({providedIn: 'root'}), inject(HttpClient), HttpParams, and strictly typed Observable return values.\n"
            "- Define and export only source-evidenced request/response/page interfaces; preserve backend JSON names and nullability exactly.\n"
            "- Use /api as the relative base, page/pageSize query names, encoded parameters, and typed HttpErrorResponse handling without swallowing failures.\n"
            "- Do not use fetch, import.meta.env, React hooks, any, invented CRUD operations, or duplicate UI state.\n"
            "Return only the complete .api.ts source. No Markdown or explanatory prose."
        )
    return (
        f"{context}\n{prod_rules}{grounding}\n\n"
        f"Generate one complete TypeScript API module for the {domain} feature using native fetch, AbortSignal support, encoded URLSearchParams, and strictly typed source-evidenced contracts. "
        "Use a relative /api base by default, report non-2xx status and response details safely, and do not invent CRUD methods absent from source evidence. "
        "If an empty response is valid, declare that explicitly as void, null, or undefined in the operation's return type; otherwise throw a descriptive error instead of returning an absent value. "
        "Use either standard function declarations with block bodies or const arrow functions with an equals sign; never mix declaration syntax with an arrow token. "
        "This is a plain .ts transport module: never emit JSX, React elements, components, hooks, or HTML; use `value as Type` rather than angle-bracket type assertions. "
        "Return only source code."
    )



# Function: _llm_domain_typescript
def _llm_domain_typescript(
    files: dict[str, str],
    domain: str,
    root_ns: str,
    domain_tables: list[str],
    context: str,
    prod_rules: str,
    source_sec: str,
    guide_sec: str,
    model: str,
    system: str,
    fw: str,
    target: dict,
    on_step: Callable[[str], None] | None,
    generate: Callable[..., str],
    on_validation: Callable[[object, int], None] | None = None,
) -> set:
    """Add TypeScript/React domain files to *files* (mutates in-place)."""
    from .._shared import _FILE_GENERATION_MAX_SECONDS, _TOKENS_COMPONENT, _TOKENS_MIGRATION, _adaptive_num_ctx
    from ..validation_orchestration import _generate_validated
    base = f"ModernizedApp/src/components/{domain}"
    component_path, service_path = _framework_file_paths(base, domain, fw)
    generated_paths = set()

    # Full CRUD page component
    try:
        if on_step:
            on_step(f"[{domain}] Generating UI component…")
        prompt = _ui_component_prompt(
            fw, domain, target, context, prod_rules, source_sec, guide_sec, domain_tables,
        )
        _rel = component_path
        _content, _result, _attempts = _generate_validated(
            prompt, model=model, system=system, max_tokens=_TOKENS_COMPONENT,
            num_ctx=_adaptive_num_ctx(len(prompt) + len(system), _TOKENS_COMPONENT),
            rel_path=_rel, language="typescript",
            max_attempts=3,
            generation_max_seconds=_FILE_GENERATION_MAX_SECONDS,
            on_token=getattr(on_step, "on_token", None),
            on_attempt=(lambda a, m, _d=domain: on_step(f"[{_d}] Fixing UI component — attempt {a}/{m}…")) if on_step else None,
        )
        if not _result.passed:
            raise RuntimeError(
                f"strict source validation failed for {_rel}: "
                + "; ".join(_result.diagnostics[:5])
            )
        files[_rel] = _content
        if on_validation:
            on_validation(_result, _attempts)
        if _result.passed:
            generated_paths.add(_rel)
    except Exception as exc:
        # Never turn an inference/validation exception into a source file.
        # Those marker comments later become governance failures, while a Java
        # Maven build can still appear green because it does not compile the
        # frontend.  Propagate the scoped failure so the domain worker retries
        # or fails the job before an invalid cache/artifact is persisted.
        raise RuntimeError(
            f"{fw} component generation failed for {component_path}: {exc}"
        ) from exc

    # API service layer
    try:
        if on_step:
            on_step(f"[{domain}] Generating API service layer…")
        prompt = _api_service_prompt(fw, domain, context, prod_rules, source_sec)
        _rel = service_path
        _content, _result, _attempts = _generate_validated(
            prompt, model=model, system=system, max_tokens=_TOKENS_MIGRATION,
            num_ctx=_adaptive_num_ctx(len(prompt) + len(system), _TOKENS_MIGRATION),
            rel_path=_rel, language="typescript",
            max_attempts=3,
            forbid_jsx=True,
            generation_max_seconds=_FILE_GENERATION_MAX_SECONDS,
            on_token=getattr(on_step, "on_token", None),
            on_attempt=(lambda a, m, _d=domain: on_step(f"[{_d}] Fixing API service layer — attempt {a}/{m}…")) if on_step else None,
        )
        if not _result.passed:
            raise RuntimeError(
                f"strict source validation failed for {_rel}: "
                + "; ".join(_result.diagnostics[:5])
            )
        files[_rel] = _content
        if on_validation:
            on_validation(_result, _attempts)
        if _result.passed:
            generated_paths.add(_rel)
    except Exception as exc:
        raise RuntimeError(
            f"{fw} API generation failed for {service_path}: {exc}"
        ) from exc
    return generated_paths
