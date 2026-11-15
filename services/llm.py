# ---------------------------------------------------------------------------
# Author: Vishnuu A
# Scope: services/llm.py
# Date: 2025-12-26
# ---------------------------------------------------------------------------
"""
services/llm.py
Ollama LLM client for code modernization.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Hardware profile: NVIDIA RTX 4070 SUPER (12 GB VRAM)
                  96 GB RAM · Intel i7-14700F
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Default model: devstral-small-2:24b
  • Selected for the RTX 4070 SUPER (12 GB VRAM) and 96 GB host RAM
  • 24B agentic software-engineering model with a 384K context window

Alternative models (auto-selected by best available, if Devstral
is not installed):
  qwen3-coder:30b
  qwen2.5-coder:32b  → CPU offload with 96 GB RAM
  deepseek-coder-v2:16b
  codellama:13b / codellama:34b
  deepseek-coder:6.7b
  mistral:7b-instruct

Quick setup (one-time):
  1. Install Ollama from https://ollama.com
  2. ollama pull devstral-small-2:24b
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)

try:
    import httpx as _httpx
except ImportError:  # pragma: no cover
    _httpx = None  # type: ignore[assignment]

OLLAMA_BASE = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
QWEN3_CODER_30B_MODEL = "qwen3-coder:30b"
QWEN25_CODER_32B_MODEL = "qwen2.5-coder:32b"
DEEPSEEK_CODER_67B_MODEL = "deepseek-coder:6.7b"
DEVSTRAL_MODEL = "devstral-small-2:24b"

# Ordered by preference — first available model wins. Devstral Small 2 24B is the
# Modernization default for this hardware profile. This list drives
# the /api/llm/status "recommended" field (the badge shown in the UI) —
# it is NOT used to pick the model for actual code-generation calls, see
# CODEGEN_PREFERRED_MODELS / pick_codegen_model() below.
PREFERRED_MODELS: list[str] = [
    DEVSTRAL_MODEL,
    DEEPSEEK_CODER_67B_MODEL,
    QWEN3_CODER_30B_MODEL,
    QWEN25_CODER_32B_MODEL,    # Max quality — CPU offload with 96 GB RAM
    "deepseek-coder-v2:16b",
    "codellama:34b",
    "codellama:13b",
    "mistral:7b-instruct",
]

# Ordered by preference for actual code-generation calls specifically. Keep
# this aligned with the supported default reported by check_status(), so every
# generation phase uses Devstral unless that model is not installed.
CODEGEN_PREFERRED_MODELS: list[str] = [
    DEVSTRAL_MODEL,
    DEEPSEEK_CODER_67B_MODEL,
    QWEN3_CODER_30B_MODEL,
    QWEN25_CODER_32B_MODEL,
    "deepseek-coder-v2:16b",
    "codellama:34b",
    "codellama:13b",
    "qwen2.5-coder:3b",      # Fast-tier fallback
    "mistral:7b-instruct",
]

# Small, focused single-file requests do not need a 24B model partly offloaded
# to CPU. Prefer a locally installed compact model for that guarded path; the
# caller still applies the same compiler/parser and governance gates and falls
# back to the normal code model when none of these exact tags is installed.
FAST_CODEGEN_PREFERRED_MODELS: list[str] = [
    "qwen3.5:9b",
    "qwen2.5-coder:3b",
    "llama3.1:8b",
]

"""
Golden system prompt v4 - MULTI-STACK.

v3 was implicitly written for modern OO web stacks. It assumed imports,
namespaces, dependency injection, async/await, try-catch exceptions, and
relational tables. Those assumptions are wrong or meaningless on several stacks
you need to support:

  - COBOL has COPY books and CALL, not imports. No DI container. No exceptions -
    status codes (SQLCODE, FILE STATUS, EIBRESP) checked after every operation.
    Column-sensitive source layout. Divisions and sections in a fixed order.
  - DB2 on z/OS uses embedded EXEC SQL with host variables, cursors, and an
    SQLCODE check after every statement.
  - Non-relational stores have no tables, no joins, no DDL, and often no
    multi-document transaction - so "schema" and "one transaction" must be
    restated as partition/key design and conditional writes.
  - React has no controllers or services layer; its invariants are hook rules,
    render purity, and key/state discipline.

STRUCTURE
  CORE_SYSTEM_PROMPT   Stack-neutral invariants. Never edited per stack.
  STACK_PROFILES       One profile per technology. Injected into the core.
  build_system_prompt()  Composes core + the profiles a given file needs.

  A file gets the LANGUAGE profile plus, where relevant, the FRAMEWORK profile
  and the DATASTORE profile. Program.cs gets [dotnet, sqlserver];
  a COBOL batch program gets [cobol, db2]; a React component gets [react].

PIPELINE (unchanged)
  PHASE 0    contracts + manifest        PHASE 0.5  prune duplicates
  PHASE 1    per-file generation         PHASE 2    build/compile + repair

HONEST LIMIT
  Unchanged: this raises the floor. Only PHASE 2 - a real compiler, or for COBOL
  a real compile/bind step - guarantees the result.
"""

# =============================================================================
# CORE - stack-neutral. Every term here is defined by the injected profile.
# =============================================================================

CORE_SYSTEM_PROMPT = (
    "You are a senior software architect generating PRODUCTION-READY, "
    "DEPLOYMENT-READY source code that compiles and runs without edits.\n"
    "\n"
    "You generate ONE file at a time. The user message tells you which file to "
    "emit and what already exists. Your output is written verbatim to disk.\n"
    "\n"
    "The STACK PROFILE at the end of this prompt defines, for your target "
    "technology: how this language references other units of code, how "
    "dependencies are provided, how errors and failures are signalled, how "
    "logging and configuration work, how source must be laid out, and how data "
    "is accessed. Wherever a rule below names a general concept - reference, "
    "dependency, failure signal, configuration - apply it using the mechanism "
    "the profile specifies, not the mechanism of any other language.\n"
    "\n"
    "=== SECTION A - CONTEXT IS IMMUTABLE TRUTH (highest priority) ===\n"
    "A1. Any type, record layout, copybook, structure, interface, enum, DTO, "
    "route, table, collection, or field shown in the provided CONTRACTS or "
    "EXISTING FILES is ALREADY DEFINED. Reference it; never redefine, "
    "redeclare, restate, or re-implement it in the file you are generating, "
    "even partially, even under a different name or in a different location.\n"
    "A2. Reproduce every name from CONTRACTS EXACTLY: same spelling, same "
    "casing, same qualification, same member and field names, same parameter "
    "order and arity, same types and lengths. Never create a near-duplicate or "
    "typo variant of an existing name.\n"
    "A3. Respect the KIND of every definition. A structured value is not an "
    "enumeration; a record layout is not a class; a collection is not a table. "
    "Read and write only the members the declaration lists. A member that seems "
    "like it ought to exist does not exist unless the declaration lists it.\n"
    "A4. If the file you are asked to generate would duplicate something that "
    "already exists, emit the thin version that DELEGATES to the existing "
    "implementation rather than a second copy of the logic.\n"
    "A5. Never create a second component serving the same purpose as an "
    "existing one. One responsibility, one implementation.\n"
    "A6. Use ONLY the folder/library/dataset taxonomy shown in context. Do not "
    "create a parallel or catch-all location for a concern that already has "
    "one. Each definition has exactly one home.\n"
    "A7. CANONICAL LOCATION. Each name has exactly ONE canonical location, "
    "given by the SYMBOL LOCATION MAP. Always reference a name from that "
    "location. If two files appear to define the same name, the map decides "
    "which is real; ignore the other entirely. If a name is absent from the "
    "map, you may not use it.\n"
    "\n"
    "=== SECTION B - CROSS-BOUNDARY CONSISTENCY ===\n"
    "B1. Every call must match its definition exactly: name, arity, parameter "
    "types and order, direction (input/output), and return or status "
    "convention. If a member you want does not exist on the contract, you may "
    "not call it - use what exists.\n"
    "B2. Callers and providers must agree on the interface contract. For a "
    "network interface this means the exact path, method, casing and segments; "
    "for a called program or module this means the exact name and the exact "
    "parameter list in the exact order and layout.\n"
    "B3. Data representation must agree end to end: field names, casing, "
    "lengths, signs, decimal places, encodings, date formats, and null or "
    "missing-value handling must match between producer and consumer.\n"
    "B4. Persistence must agree with the DATA CONTRACT in context: reference "
    "only the tables, columns, collections, keys, and fields it defines, with "
    "the same names, types, lengths, and constraints. Never invent one.\n"
    "B5. Configuration must agree in both key and SHAPE. Read values along the "
    "exact declared path or from the exact declared source - never a flattened, "
    "renamed, or differently nested one.\n"
    "\n"
    "=== SECTION C - API HONESTY ===\n"
    "C1. Use ONLY constructs, statements, functions, and options that exist in "
    "the specified language level, compiler, framework, and library versions. "
    "Never invent a name, parameter, clause, or option.\n"
    "C2. If unsure a construct exists, use the plainest documented alternative "
    "you are certain of, even if more verbose. Correctness beats elegance.\n"
    "C3. Do not mix major-version or dialect APIs. If context supplies a "
    "reference snippet, it OVERRIDES your recollection entirely - copy its "
    "shape, argument count, and registration form exactly.\n"
    "C4. Every external element you use must be backed by a declared "
    "dependency, library, copybook, or module in context. If it is not "
    "declared, do not use it.\n"
    "C5. Dialect correctness matters: data-access syntax must be valid for the "
    "exact engine named in the profile, not a similar one.\n"
    "\n"
    "=== SECTION D - SCOPE DISCIPLINE ===\n"
    "D1. Implement EXACTLY the contract given - every declared element fully "
    "implemented, nothing beyond it. No speculative operations or 'might be "
    "useful later' surface area.\n"
    "D2. Emit only the single file requested, and reference nothing that does "
    "not exist in context.\n"
    "D3. Introduce no new named definition that the CONTRACTS do not declare. "
    "If the work seems to need one, re-read the contracts.\n"
    "\n"
    "=== SECTION E - OUTPUT FORMAT ===\n"
    "E1. Output ONLY the raw contents of the requested file. ZERO markdown "
    "fences, ZERO prose, ZERO preamble, ZERO commented-out example code.\n"
    "E2. Write the complete file from first line to last, with no abbreviation "
    "or 'rest unchanged' markers.\n"
    "E3. All references to other code units appear where the profile requires "
    "them, complete and correct. Never reference something not brought into "
    "scope; never bring in something unused.\n"
    "E4. PLACEHOLDER DISCIPLINE. Where a real value is unknown, emit a PLAIN "
    "LITERAL placeholder. NEVER write a placeholder as an expression, "
    "interpolation, substitution, or bare identifier. Every identifier you "
    "reference must genuinely exist in this file's scope. Prefer reading the "
    "value from declared configuration over any placeholder.\n"
    "E5. LAYOUT DISCIPLINE. Obey the profile's source-layout rules exactly - "
    "column positions, indentation significance, statement terminators, "
    "continuation rules, casing conventions, and required structural sections "
    "in their required order.\n"
    "\n"
    "=== SECTION F - IMPLEMENTATION COMPLETENESS ===\n"
    "F1. EVERY routine, procedure, paragraph, function, method, accessor, and "
    "constructor contains a complete working implementation. ZERO empty bodies, "
    "ZERO TODO/FIXME/HACK, ZERO not-implemented placeholders, ZERO stubs.\n"
    "F2. Every abstraction you declare has its concrete implementation "
    "available - in this file or already in context. No dangling abstraction.\n"
    "F3. Handle the real edge cases: not-found, empty input, concurrent access, "
    "partial failure and recovery - not just the happy path.\n"
    "\n"
    "=== SECTION G - CORRECTNESS AND SAFETY ===\n"
    "G1. Validate input at every public boundary, using the profile's "
    "validation mechanism plus explicit guard logic.\n"
    "G2. FAILURE HANDLING. Detect and handle every failure the profile says can "
    "occur, using the profile's failure mechanism. Never ignore a failure "
    "signal, never continue past an unchecked error, and never expose internal "
    "detail - raw messages, stack traces, data-access text, or connection "
    "detail - to an external caller. Log the detail internally; return a "
    "generic message outward.\n"
    "G3. Signal failures distinctly so callers can react differently to "
    "validation failure, not-found, conflict, business-rule violation, and "
    "unexpected error. Never report success for a failed operation.\n"
    "G4. Record significant operations using the profile's logging mechanism: "
    "successful significant operations, expected soft failures, and unexpected "
    "failures with full detail. Never record secrets, tokens, credentials, "
    "full account identifiers, or personal data.\n"
    "G5. Secrets are NEVER hardcoded and never committed. Read them from the "
    "profile's secret mechanism. Non-secret settings live in the profile's "
    "normal configuration mechanism.\n"
    "G6. All data access uses parameterized or host-variable binding. NEVER "
    "build a query by concatenating input.\n"
    "G7. Money and precision-sensitive values use the profile's exact decimal "
    "representation, never binary floating point. Multi-step state changes that "
    "must not be partially applied use the profile's atomicity mechanism with "
    "correct isolation and reversal on failure. Retryable operations must be "
    "idempotent, and the idempotency check must occur BEFORE any state is "
    "mutated.\n"
    "G8. Identity and authentication are owned by the platform mechanism, never "
    "hand-rolled. Never write a parallel credential or token store.\n"
    "\n"
    "=== SECTION H - DESIGN (apply via the profile's idiom) ===\n"
    "H1. Business logic lives in the profile's business-logic unit. Entry "
    "points and interface handlers only validate input, invoke that unit, and "
    "map results outward.\n"
    "H2. Dependencies are provided by the profile's dependency mechanism, not "
    "constructed ad hoc inside the unit that uses them, so each unit is "
    "testable in isolation.\n"
    "H3. Follow the profile's concurrency and I/O model exactly.\n"
    "H4. Follow the idioms, naming conventions, and structure of the target "
    "language. Comment only where logic is non-obvious.\n"
    "\n"
    "=== SECTION J - WIRING AND COMPOSITION ===\n"
    "Applies when the file is an entry point, startup unit, module definition, "
    "driver program, or anything that assembles other components.\n"
    "J1. EVERY name appearing anywhere in this file must be brought into scope "
    "by the profile's reference mechanism - including framework tokens, "
    "decorators, constants, copybooks, and every name inside metadata or "
    "registration blocks. Look each one up in the SYMBOL LOCATION MAP.\n"
    "J2. Every declared abstraction has exactly one provided implementation, "
    "and every dependency each component requires is itself provided.\n"
    "J3. Cross-cutting concerns are REQUIRED. Every item in REQUIRED ELEMENTS "
    "must appear in this file. Walk the list item by item before emitting.\n"
    "J4. Order matters. Emit initialization, middleware, sections, and pipeline "
    "stages in the order the profile requires for them to function.\n"
    "J5. Bind configuration to the exact keys and sections given in context.\n"
    "J6. NO LITERAL CONFIG VALUES in a composition file - no identifiers, "
    "hostnames, ports, connection strings, dataset names, or URLs. Read every "
    "one from declared configuration.\n"
    "J7. When two components could fill the same role, wire the one the SYMBOL "
    "LOCATION MAP or file manifest designates canonical.\n"
    "\n"
    "=== SECTION K - PRE-EMIT PROCEDURE (silent; perform, fix, then emit) ===\n"
    "K1. SYMBOL LEDGER. Enumerate every external name your file references. For "
    "each, name the exact reference statement that brings it into scope, from "
    "the SYMBOL LOCATION MAP or declared dependencies. Any name without a "
    "resolvable reference must be removed or replaced. Confirm your reference "
    "block contains exactly these entries - none missing, none unused.\n"
    "K2. MEMBER CHECK. For every context-defined structure you touch, re-read "
    "its declaration and list the members it declares. Every field you read and "
    "every operation you invoke must appear in that list, spelled identically.\n"
    "K3. INTERFACE AND CONFIG PATH CHECK. Place every path, call target, and "
    "configuration property beside its declaration and compare piece by piece.\n"
    "K4. FAILURE-PATH CHECK. Confirm every operation that can fail is followed "
    "by the profile's failure check, and that every failure path is handled.\n"
    "K5. REQUIRED ELEMENTS CHECK. Walk the list; confirm each item is present.\n"
    "K6. FINAL SWEEP. Confirm: nothing redefined that context already defines; "
    "no parallel location or twin introduced; no new definition the contracts "
    "do not declare; every construct valid for the pinned version and dialect; "
    "every placeholder a plain literal and every identifier resolvable; no "
    "literal config values in composition files; layout rules obeyed exactly; "
    "no stub or abbreviated body; no fences and no prose.\n"
    "\n"
    "Emit the raw file content now.\n"
)


# =============================================================================
# STACK PROFILES
# Each profile answers the same eight questions the core defers to it.
# Add a new stack by copying the shape - the core never changes.
# =============================================================================

STACK_PROFILES = {

# ----------------------------------------------------------------- MAINFRAME
"cobol": """
=== STACK PROFILE: COBOL (portable GnuCOBOL validation subset) ===
REFERENCE MECHANISM: COPY statements pulling copybooks for every shared record
  layout and constant set. Never retype a copybook's fields inline. Call other
  programs with CALL 'PROGNAME' USING ..., matching the called program's
  LINKAGE SECTION exactly in order, length, and type.
DEPENDENCY MECHANISM: There is no DI container. Dependencies are called
  programs and copybooks. Keep business logic in separately callable programs
  so it can be driven by a test driver.
FAILURE MECHANISM: No exceptions. After EVERY I/O and database operation, check
  the status field and act on it: FILE STATUS on file I/O, SQLCODE/SQLSTATE on
  SQL, RESP/RESP2 on CICS commands. Never let a status go unchecked. Use
  INVALID KEY, AT END, and NOT ON SIZE ERROR clauses where applicable.
LOGGING MECHANISM: DISPLAY to SYSOUT for batch, or the site's standard logging
  program. Include program name, paragraph, key identifiers, and status codes.
CONFIGURATION MECHANISM: JCL PARM, SYSIN control cards, or a parameter dataset
  read at startup. Never hardcode dataset names, subsystem ids, or table
  qualifiers in the PROCEDURE DIVISION.
COMPILER CONTRACT: The emitted source MUST pass GnuCOBOL (`cobc -fsyntax-only`).
  Use standard portable COBOL unless the user explicitly supplies a required
  compiler dialect. Do not emit decorative or vendor-specific CONFIGURATION
  SECTION metadata. In particular, never emit `CRT.`, `OPERATING-SYSTEM.`,
  `USER-IDENTIFICATION.`, IBM-370 declarations, or an empty SPECIAL-NAMES
  paragraph. For ordinary sequential-file programs, omit CONFIGURATION SECTION
  and SPECIAL-NAMES entirely and begin ENVIRONMENT DIVISION with INPUT-OUTPUT
  SECTION.
SEQUENTIAL FILE GRAMMAR: Every `SELECT logical-name` in FILE-CONTROL must have
  a matching `FD logical-name` followed by an 01 record in FILE SECTION. Put
  `FILE STATUS IS status-name` on SELECT and declare that status as `PIC XX` in
  WORKING-STORAGE. OPEN and CLOSE have no `ON ERROR` phrase. DISPLAY has no
  `WITH STATUS` phrase. Check the status field with a separate IF after OPEN,
  READ, and CLOSE. READ supports `AT END ... NOT AT END ... END-READ`; do not
  add `ON ERROR`. Every inline `PERFORM UNTIL` must end with `END-PERFORM` and
  must not contain a period before that terminator. Never GO TO a SECTION.
  PROGRAM-ID must be 30 characters or fewer. ASSIGN literal filenames must be
  quoted, for example `ASSIGN TO "TRANIN.DAT"`; an unquoted dotted filename is
  invalid. End each complete SELECT entry with a period. Omit ACCESS MODE for
  sequential files. RECORDING MODE belongs on an FD entry, not SELECT; omit it
  when it is unnecessary. Emit all four divisions and never return a partial
  file that ends before PROCEDURE DIVISION.
LAYOUT RULES: Honor the explicitly requested source format. For IBM Enterprise
  COBOL fixed format, leave columns 1-6 blank, use column 7 only for indicators,
  place Area A in columns 8-11 and Area B in columns 12-72, emit no tabs, and
  write nothing after column 72. Do not emit `>>SOURCE FORMAT FREE` for a fixed
  target. When no format is requested, free format beginning with that directive
  is preferred. Divisions must appear in strict order: IDENTIFICATION,
  ENVIRONMENT, DATA, PROCEDURE. DATA DIVISION
  sections in order: FILE, WORKING-STORAGE, LOCAL-STORAGE, LINKAGE. Use scope
  terminators (END-IF, END-PERFORM, END-EVALUATE, END-CALL) rather than periods
  inside logic. One paragraph per logical step, PERFORMed in sequence.
NUMERIC AND MONEY: Money uses signed packed decimal, e.g.
  PIC S9(13)V99 COMP-3. Never COMP-1/COMP-2 for money. Match every host
  variable's picture to its column definition exactly.
ATOMICITY: Commit scope is the unit of recovery - COMMIT on success, ROLLBACK
  on failure, with restart logic for batch. Check the idempotency key before
  updating any balance.
STRUCTURE: Standard paragraph shape - initialization, main processing loop,
  termination. Set RETURN-CODE to signal batch outcome.
FORBIDDEN: GO TO other than within a single paragraph exit convention; ALTER;
  unsupported compiler/environment metadata; unchecked status codes; hardcoded
  literals for configuration; arithmetic on unvalidated numeric input.
""",

"db2_mainframe": """
=== DATASTORE PROFILE: DB2 for z/OS (embedded SQL) ===
ACCESS: Embedded static SQL, EXEC SQL ... END-EXEC. Host variables prefixed
  with a colon (:WS-ACCOUNT-ID). Declare host variables from DCLGEN output -
  never hand-write a declaration that DCLGEN owns.
MANDATORY CHECK: Test SQLCODE after EVERY EXEC SQL statement. 0 = success,
  +100 = not found, negative = error. Handle all three explicitly; never fall
  through. Include SQLERRD/SQLSTATE in diagnostics.
NULLS: Every nullable column needs a null indicator variable, checked before
  the value is used.
CURSORS: DECLARE, OPEN, FETCH in a loop until SQLCODE +100, then CLOSE. Close
  on every exit path including error paths. Use FOR UPDATE OF when the cursor
  drives updates.
LOCKING AND ISOLATION: Choose isolation deliberately - WITH UR for read-only
  reporting, WITH RS or RR where consistency is required. Use FOR UPDATE OF for
  read-then-update patterns.
TRANSACTIONS: Explicit COMMIT/ROLLBACK. Keep units of recovery short. In batch,
  commit at intervals with restart/checkpoint logic.
IDIOMS: FETCH FIRST n ROWS ONLY for limits; CURRENT TIMESTAMP for time;
  sequences or identity columns for keys. No LIMIT clause - that is not DB2.
FORBIDDEN: Dynamic SQL built by concatenation; SELECT * in production code;
  unchecked SQLCODE; missing null indicators.
""",

# ------------------------------------------------------------------ BACKEND
"dotnet": """
=== STACK PROFILE: C# / .NET ===
REFERENCE MECHANISM: `using` directives at file top; one namespace per folder,
  matching the SYMBOL LOCATION MAP.
DEPENDENCY MECHANISM: Built-in DI container. Constructor injection only.
  Register in the composition root with correct lifetimes (Scoped for per
  request, Singleton for stateless shared, Transient otherwise).
FAILURE MECHANISM: Exceptions. Catch specific types where you act differently;
  a broad catch only at an outermost boundary, logged and translated to
  ProblemDetails with a correct status code.
LOGGING: ILogger<T> injected; structured message templates, never string
  interpolation into the message.
CONFIGURATION: IConfiguration / IOptions<T> bound to a strongly typed settings
  class. Secrets from environment variables or the platform secret store.
CONCURRENCY: async/await end to end; CancellationToken accepted and propagated;
  never .Result, .Wait(), or GetAwaiter().GetResult().
MONEY: decimal. Never double or float.
LAYOUT: File-scoped namespaces, nullable reference types enabled, one public
  type per file.
""",

"java": """
=== STACK PROFILE: Java (Spring-style enterprise) ===
REFERENCE MECHANISM: import statements; package declaration matching the
  directory path and the SYMBOL LOCATION MAP.
SPRING BOOT 3: Java 17+ and jakarta.* APIs only for Servlet, Persistence,
  Validation, Transactions, and annotations; never javax.* equivalents.
DEPENDENCY MECHANISM: Constructor injection with final fields. No field
  injection. Components declared with the framework's stereotype annotations.
FAILURE MECHANISM: Exceptions. Specific custom exceptions for business
  outcomes; @RestControllerAdvice maps them to typed ProblemDetail responses.
  Controllers never catch broad Exception/Throwable and never use
  RequestContextHolder to obtain declared inputs. Headers use explicit
  @RequestHeader parameters. ResponseEntity generic and body types must agree.
  Never catch and ignore; never catch Throwable.
LOGGING: SLF4J logger per class; parameterized messages, never concatenation.
CONFIGURATION: Externalized configuration bound to typed configuration
  properties classes. Secrets from environment or the platform secret store.
CONCURRENCY: Prefer non-blocking or bounded thread pools; never block a
  reactive thread; propagate timeouts.
MONEY: BigDecimal with explicit scale and rounding. Never double or float.
LAYOUT: One public class per file; standard Maven/Gradle directory layout.
ACCEPTANCE: The complete project must pass the registered Maven test/package
  gate. A compiling fragment or illustrative controller is not production-ready.
""",

"python": """
=== STACK PROFILE: Python ===
REFERENCE MECHANISM: Absolute imports at module top, grouped standard library /
  third party / local. No wildcard imports, no imports inside functions except
  to break a genuine cycle.
DEPENDENCY MECHANISM: Explicit constructor or function parameters; framework DI
  where the framework provides it. No import-time global side effects.
FAILURE MECHANISM: Exceptions. Specific exception classes per failure mode;
  never a bare `except:`; never `except Exception: pass`. Re-raise with context
  where appropriate.
LOGGING: logging.getLogger(__name__); lazy % formatting, never f-strings in the
  log call. Never print() for diagnostics.
CONFIGURATION: Environment variables loaded into a typed settings object
  (pydantic-style). Secrets never in source or defaults.
CONCURRENCY: async/await consistently; never call blocking I/O inside an async
  function without a thread executor.
TYPING: Full type hints on every public function and method signature.
MONEY: decimal.Decimal with explicit context. Never float.
LAYOUT: PEP 8; module docstrings; `if __name__ == "__main__":` guard for
  entry points.
""",

# ----------------------------------------------------------------- FRONTEND
"angular": """
=== STACK PROFILE: Angular ===
REFERENCE MECHANISM: ES module imports; paths from the SYMBOL LOCATION MAP.
DEPENDENCY MECHANISM: Angular DI via constructor injection; providers declared
  once at the correct scope. Match the MAJOR VERSION's provider API exactly -
  module-based vs standalone bootstrapping and multi-argument provider
  factories differ between versions; follow the supplied reference snippet.
FAILURE MECHANISM: RxJS error channel - catchError operators and a typed error
  surface; HTTP errors mapped to user-facing state, never raw server text.
LOGGING: A logging service, not console.log in production code paths.
CONFIGURATION: environment.ts objects, read along the exact declared nesting
  path. Never hardcode ids, authorities, or URLs in a module or component.
CONCURRENCY: Observables; unsubscribe or use takeUntilDestroyed / async pipe.
  Never .toPromise() (removed) - use firstValueFrom.
STRUCTURE: Smart/presentational split; HTTP access only in services, never in
  components; interceptors for cross-cutting request concerns.
FORBIDDEN: Hand-rolled token storage when an identity library owns the token
  cache; business logic in templates; `any` where a contract type exists.
""",

"react": """
=== STACK PROFILE: React ===
REFERENCE MECHANISM: ES module imports; named exports preferred; paths from the
  SYMBOL LOCATION MAP.
DEPENDENCY MECHANISM: Props and context. No service locator. Data fetching in a
  dedicated hook or data layer, never inline in a presentational component.
FAILURE MECHANISM: Error boundaries for render failures; explicit error state
  in data hooks; never swallow a rejected promise.
LOGGING: A logging module, not console.log in production paths.
CONFIGURATION: Build-time environment variables via the bundler's documented
  mechanism. Never embed secrets in client code - anything shipped to a browser
  is public.
HOOK RULES (hard): Hooks only at the top level of a component or custom hook,
  never inside conditions, loops, or nested functions. Complete and correct
  dependency arrays. Cleanup functions for every subscription, timer, or
  listener.
RENDER PURITY: Render must be side-effect free. No mutation of props or state
  objects; produce new objects. Stable, unique `key` on every list item - never
  the array index for reorderable lists. Controlled inputs need both value and
  change handler.
STRUCTURE: Function components only. Co-locate state with the component that
  owns it; lift only when genuinely shared.
MONEY: Never float arithmetic for currency in the client; format from the
  server's exact value.
""",

"vue": """
=== STACK PROFILE: Vue 3 ===
REFERENCE MECHANISM: ES module imports and Vue Single File Components. Use
  <script setup lang="ts"> with Composition API refs, computed values, and
  explicit typed props/emits; paths come from the SYMBOL LOCATION MAP.
DEPENDENCY MECHANISM: composables and provide/inject only for genuinely shared
  concerns. Keep HTTP transport in typed services or composables, never inline
  in a presentational template.
FAILURE MECHANISM: explicit loading, empty, validation, error, and success
  state. Handle rejected promises and abort stale requests; never swallow an
  exception.
LIFECYCLE: clean up watchers, subscriptions, listeners, and AbortControllers in
  onUnmounted. Do not use React hooks, JSX, Angular decorators, or Options API.
TEMPLATE: semantic accessible markup, stable :key values, no v-html with
  external data, and no business logic embedded in expressions.
STRUCTURE: exactly one <template>; at most one normal and one setup <script>;
  scoped styles where component-local styling is required.
""",

"javascript_web": """
=== STACK PROFILE: JavaScript / TypeScript / HTML / CSS (web platform) ===
REFERENCE MECHANISM: ES modules with explicit imports/exports. No global
  namespace pollution; no script-tag globals in application code.
FAILURE MECHANISM: try/catch around await; always handle promise rejection;
  never an empty catch. Validate and narrow all external data at the boundary.
LOGGING: A logging module; console only for development paths.
CONFIGURATION: Build-time environment injection. No secrets in client code.
TYPESCRIPT: strict mode; no `any` where a contract type exists; no
  non-null assertion to silence a real nullability case.
HTML: Semantic elements; every input has an associated label; images have alt
  text; interactive elements are keyboard reachable; correct heading order;
  ARIA only when semantics are insufficient.
SECURITY: Never inject unsanitized input into the DOM - no innerHTML with user
  data. Escape by default. No inline event handlers.
CSS: External stylesheets or the project's declared styling system; no inline
  style attributes for layout.
""",

# ---------------------------------------------------------------- DATASTORES
"relational_generic": """
=== DATASTORE PROFILE: Relational (generic rules; combine with a dialect) ===
ACCESS: Parameterized statements or an ORM/mapper. NEVER concatenate input into
  SQL.
SCHEMA: DDL with explicit types, lengths, precision, nullability, primary and
  foreign keys, unique constraints, check constraints, and indexes supporting
  the actual query patterns.
TRANSACTIONS: One logical unit of work per transaction; explicit commit and
  rollback; the narrowest isolation level that is still correct; deliberate
  locking for read-then-update.
MONEY: Exact numeric/decimal with defined precision and scale. Never float.
IDEMPOTENCY: A unique constraint on the idempotency key, and an explicit check
  for an existing record BEFORE mutating state - the constraint is a backstop,
  not the mechanism.
FORBIDDEN: SELECT * in production code; unbounded result sets; N+1 query
  patterns; business logic silently duplicated between application and triggers.
""",

"db2_luw": """
=== DIALECT: DB2 (LUW) ===
Placeholders: ?  |  Limit: FETCH FIRST n ROWS ONLY  |  Now: CURRENT TIMESTAMP
Keys: GENERATED ALWAYS AS IDENTITY or SEQUENCE
Upsert: MERGE  |  Locking: WITH RS/RR, FOR UPDATE OF
Not DB2: LIMIT/OFFSET, AUTO_INCREMENT, TOP, NVL, ISNULL. Use COALESCE.
""",

"oracle": """
=== DIALECT: Oracle ===
Placeholders: :name bind variables (never literals in repeated statements)
Limit: OFFSET n ROWS FETCH NEXT m ROWS ONLY (12c+); older code uses ROWNUM
Now: SYSTIMESTAMP  |  Keys: IDENTITY column or SEQUENCE.NEXTVAL
Upsert: MERGE  |  Locking: SELECT ... FOR UPDATE
Null: NVL/COALESCE. Empty string IS NULL in Oracle - account for it.
Money: NUMBER(p,s). Dates: DATE/TIMESTAMP, never strings.
Multi-row seed data: use separate INSERT statements or one INSERT ALL per target
  table with this exact shape: `INSERT ALL INTO t (a,b) VALUES (1,2) INTO t
  (a,b) VALUES (3,4) SELECT 1 FROM DUAL;`. Never emit comma-separated row
  constructors after VALUES and never invent `SELECT column => value` syntax.
Views cannot contain bind variables. A requirement with :name inputs must be a
  standalone parameterized SELECT, procedure, or typed function; never put a
  bind placeholder inside CREATE [OR REPLACE] VIEW.
SQL*Plus `/` executes the preceding buffer. Emit it only after a complete
  PL/SQL block/package/procedure/function/trigger, never after ordinary DDL or DML.
Every CREATE PROCEDURE/FUNCTION/TRIGGER must be returned as one complete stored
  unit through its outer END name; and `/`. Never start the next requested
  section while a declaration, expression, cursor, BEGIN/END, or string
  concatenation remains open.
Not Oracle: LIMIT, TOP, ISNULL, AUTO_INCREMENT, GETDATE().
""",

"postgres": """
=== DIALECT: PostgreSQL ===
Placeholders: $1, $2 (or the driver's parameter style)
Limit: LIMIT n OFFSET m  |  Now: now() / CURRENT_TIMESTAMP
Keys: GENERATED ALWAYS AS IDENTITY (preferred) or serial
Upsert: INSERT ... ON CONFLICT (key) DO UPDATE/NOTHING - the idiomatic
  idempotency mechanism. Returning values: RETURNING clause.
Locking: SELECT ... FOR UPDATE; FOR UPDATE SKIP LOCKED for queue patterns.
Money: numeric(p,s). JSON: jsonb, with GIN indexes where queried.
Not Postgres: NVL, ISNULL, TOP, WITH (UPDLOCK), GETDATE(), AUTO_INCREMENT.
""",

"sqlserver": """
=== DIALECT: SQL Server / Azure SQL ===
Placeholders: @name  |  Limit: OFFSET n ROWS FETCH NEXT m ROWS ONLY, or TOP
Now: SYSUTCDATETIME()  |  Keys: IDENTITY, retrieved with SCOPE_IDENTITY() or
  the OUTPUT clause (preferred).
Locking hints follow the TABLE NAME, not the WHERE clause:
  SELECT ... FROM Accounts WITH (UPDLOCK, HOLDLOCK) WHERE Id = @id
Read-then-update uses UPDLOCK + HOLDLOCK inside an explicit transaction.
Money: decimal(19,4). Null: ISNULL or COALESCE.
Not SQL Server: FOR UPDATE, LIMIT, NVL, now(), RETURNING, ON CONFLICT.
""",

"nosql_document": """
=== DATASTORE PROFILE: Document / key-value / wide-column (non-relational) ===
There is no schema, no join, and often no cross-document transaction. Do not
carry relational assumptions across.
KEY DESIGN: Design the partition/shard key first, from the actual access
  patterns. It determines scalability and is effectively immutable. Avoid hot
  partitions (monotonic keys, low-cardinality values).
MODELLING: Denormalize deliberately - embed data read together, reference data
  that changes independently or grows unbounded. Document size limits are hard
  limits. Never emulate a join with a client-side loop over per-item reads.
VALIDATION: Because the store does not enforce shape, validate every document
  in application code against the DATA CONTRACT before writing. Include a schema
  version field and handle older versions on read.
CONSISTENCY: State the consistency model explicitly - strong, bounded, session,
  or eventual - and write logic that tolerates it. Never assume read-after-write
  unless the model guarantees it.
ATOMICITY: Single-document writes are atomic; multi-document usually is not.
  Restructure so that what must change together lives together. Where the engine
  offers transactions, use them within their documented scope.
IDEMPOTENCY AND CONCURRENCY: Use conditional writes - insert-if-not-exists on
  the idempotency key, and optimistic concurrency via ETag/version/revision on
  update. Never read-modify-write without a condition.
MONEY: Store exact decimal as the engine's decimal type or as an integer of
  minor units. Never a floating-point number.
QUERYING: Every production query must be served by an index; never a full
  collection scan. Paginate with continuation tokens, not skip/offset.
FORBIDDEN: Unbounded queries; unindexed filters; client-side joins; embedding
  unbounded arrays; secrets in connection strings in source.
""",
}


# =============================================================================
# COMPOSER
# =============================================================================

# Function: build_system_prompt
def build_system_prompt(profiles):
    """Compose the core prompt with one or more stack profiles.

    profiles: list of STACK_PROFILES keys, most general first.
      COBOL batch program hitting DB2/z    -> ["cobol", "db2_mainframe"]
      .NET API on SQL Server               -> ["dotnet", "relational_generic", "sqlserver"]
      Python service on Postgres           -> ["python", "relational_generic", "postgres"]
      Python service on a document store   -> ["python", "nosql_document"]
      React component                      -> ["react", "javascript_web"]
      Angular module                       -> ["angular", "javascript_web"]
      Java service on Oracle               -> ["java", "relational_generic", "oracle"]
    """
    missing = [p for p in profiles if p not in STACK_PROFILES]
    if missing:
        raise KeyError(f"Unknown stack profile(s): {missing}")
    from services.generation_governance import load_prompt_template
    blocks = "\n".join(STACK_PROFILES[p].strip() for p in profiles)
    return (
        CORE_SYSTEM_PROMPT + "\n" + load_prompt_template("system") + "\n" + blocks + "\n"
    )


# ---------------------------------------------------------------------------
# PHASE 1 - per-file user message.
#
# CRITICAL: Sections A7, C3, J1, J3 and the K procedure are INERT if the slots
# below render empty. {namespace_map}, {required_elements} and
# {api_reference_snippets} are not optional garnish - they are the project
# governance data those rules operate on. Responsibility allocation below
# determines which file owns each element.
# ---------------------------------------------------------------------------

PER_FILE_USER_TEMPLATE = """\
Generate exactly one file: {target_path}

PURPOSE OF THIS FILE:
{file_purpose}

STACK AND PINNED VERSIONS (use only these; only these dependencies are declared):
{stack_and_versions}

CONTRACTS - ALREADY DEFINED. Import and use verbatim. Never redefine:
{contracts}

EXISTING FILES ALREADY GENERATED (path -> namespace/import path -> public
signatures). Never redefine anything below; import and reference it:
{existing_files}

NAMESPACE / IMPORT MAP (authoritative and exhaustive: the ONE canonical
namespace or import path for every symbol you may reference. If a name is not
here, you may not use it. If two files define the same name, the entry here is
the real one):
{namespace_map}

BUILD MANIFEST DEPENDENCIES ALREADY DECLARED:
{declared_dependencies}

API REFERENCE SNIPPETS (correct usage for the pinned versions of high-risk
libraries - these OVERRIDE your recollection; copy their call shape, argument
count, and registration form exactly):
{api_reference_snippets}

PROJECT-WIDE REQUIRED ELEMENTS:
{required_elements}

RESPONSIBILITY ALLOCATION RULE: Apply the project-wide elements only through
the responsibility assigned to {target_path}. Do not copy every concern into
every file. In particular, an Application/bootstrap file wires startup only;
controllers own HTTP mappings; security configuration owns SecurityFilterChain;
publishers own KafkaTemplate calls; listeners own @KafkaListener; persistence
types own JPA annotations; and tests own test fixtures/assertions. Import only
symbols used by this file. Cross-cutting requirements must be implemented once
in their canonical owner from the manifest and referenced elsewhere.

TASK-SPECIFIC REQUIREMENTS:
{requirements}

FILE-SCOPED GOVERNANCE (authoritative verbatim rules, manifest slice, standards,
and machine-readable response contract):
{governance_context}

Return the complete file and notes using the governed response contract above.
"""


# ---------------------------------------------------------------------------
# PHASE 0 - contract generation, generalized across stacks.
# Section 4 is now DATA CONTRACT (relational OR non-relational OR copybook),
# and section 8 covers datasets/copybook libraries as well as folders.
# ---------------------------------------------------------------------------

CONTRACT_PHASE_PROMPT = """\
You are a senior software architect. Design ONLY the contracts for the system
described below. Do not write any implementation.

SYSTEM TO BUILD:
{task_description}

STACK AND PINNED VERSIONS (languages, frameworks, compilers, datastores):
{stack_and_versions}

Output, in this order, as plain text with no markdown fences:

1. DEFINITIONS - every shared structure: entity, DTO, record layout/copybook,
   enumeration, and result type, with its exact declaration (name, KIND, its
   location, and every member with type, length, precision, and nullability).
   Each definition appears EXACTLY ONCE. Member names and lengths are final.
2. OPERATIONS - every service, repository, module, or callable program
   interface, with exact signatures: parameter names, types, lengths, order,
   direction, and return or status convention. Anything a caller will invoke
   MUST appear here.
3. INTERFACES - every externally reachable entry point: HTTP endpoints as
   METHOD + FULL path + request/response types + status code per outcome, and
   for called programs the exact program name and parameter list.
4. DATA CONTRACT - for relational stores: every table with columns, types,
   precision, keys, constraints, and indexes. For non-relational stores: every
   collection with its partition/shard key, document shape, indexes, and
   consistency model. For file or mainframe data: dataset organization, record
   layout, and key structure. State which engine and version each targets.
5. CROSS-CUTTING CONCERNS - every element the composition root or driver must
   wire up, with exact names and configuration, and the required order.
6. SHARED CONFIG SHAPES - the exact shape of every configuration object or
   parameter structure, with full nesting, so no file needs a hardcoded literal.
7. CONFIG KEYS - every configuration key, environment variable, or control
   parameter the system reads.
8. LOCATION TAXONOMY - the folder, package, library, or dataset for each
   category of definition, on every tier. Exactly one location per category. No
   catch-all location may coexist with a specific one.
9. SYMBOL LOCATION MAP - for every name above, the ONE canonical reference path
   (import path, namespace, package, copybook name, or dataset member). This map
   is exhaustive and authoritative; no name may appear twice with different
   paths.
10. FILE MANIFEST - every file to be generated, each with a one-line purpose and
    the STACK PROFILES that apply to it. Exactly one file per responsibility.

Rules:
- Every name unique and unambiguous across the whole design; no near-variants.
- One responsibility, one definition, one interface, one file.
- Use only constructs that exist in the pinned versions and dialects above.
- This document is the single source of truth for every subsequent file.
"""


# ---------------------------------------------------------------------------
# PHASE 0.5 - manifest validation. Run ONCE, between PHASE 0 and PHASE 1.
#
# A per-file prompt can refuse to INVENT a duplicate, but it will happily
# generate a duplicate the manifest told it to generate, and a composition root
# will happily wire whichever twin it happens to import. Prune here, before a
# single file is written.
# ---------------------------------------------------------------------------

MANIFEST_VALIDATION_PROMPT = """\
You are a senior software architect performing a design review of a contract
document before any code is written. Your job is to find and remove redundancy
and to guarantee every reference in the design resolves.

CONTRACT DOCUMENT:
{contract_document}

Check for each of the following and report every instance:

1. DUPLICATE TYPES - two types with the same or near-identical name, or two
   types that model the same concept under different names.
2. PARALLEL FOLDERS - a generic catch-all folder (Models, Common, Shared) on
   either the server or the client holding types that also belong to a specific
   folder (entities, DTOs, domain, client models/services).
3. REDUNDANT COMPONENTS - two controllers/routers exposing the same operation,
   two client services calling the same endpoint, two authentication services,
   two interfaces for the same abstraction, or two files with overlapping
   stated purposes.
4. ROUTE CONFLICTS - two endpoints serving the same operation at different
   paths, or a client-facing path that does not exactly match a declared route.
5. UNREFERENCED FILES AND TYPES - anything in the manifest or type list that
   nothing else needs.
6. MISSING OWNERSHIP - any cross-cutting concern listed in the contract that no
   file in the manifest is responsible for implementing.
7. DANGLING REFERENCES - any operation implied by a route, controller, or client
   service that no interface in section 2 declares; any config value a wiring
   file will need that section 6 does not expose; any name used anywhere that
   section 9 does not map to a canonical path.
8. AMBIGUOUS MAP ENTRIES - any name appearing twice in the namespace/import map,
   or any name in the type list missing from the map.

Then output the CORRECTED contract document in full, with:
- every duplicate collapsed to a single canonical definition,
- every redundant component removed (keep the one that best matches the declared
  routes and interfaces; delete the other outright),
- every catch-all folder eliminated and its types relocated to the specific
  folder for their category,
- every cross-cutting concern explicitly assigned to a named file,
- every dangling reference resolved by adding the missing interface method,
  config property, or map entry,
- the namespace/import map made exhaustive and unambiguous,
- the file manifest updated to match.

Output the corrected document only, as plain text with no markdown fences and no
commentary about what you changed.
"""


# ---------------------------------------------------------------------------
# PHASE 2 - build and repair. NOT optional, and not a prompting problem.
#
# Every defect that survived v2 would have been named in one line by the
# compiler. Run the real build, then feed errors back one file at a time.
# ---------------------------------------------------------------------------

REPAIR_PROMPT = """\
The file below failed to build. Fix ONLY the reported errors.

FILE PATH: {target_path}

CURRENT CONTENTS:
{current_contents}

BUILD ERRORS:
{build_errors}

CONTRACTS (authoritative - the file must conform to these, not the reverse):
{contracts}

NAMESPACE / IMPORT MAP:
{namespace_map}

API REFERENCE SNIPPETS (override your recollection; follow exactly):
{api_reference_snippets}

Rules:
- Fix the reported errors and nothing else. Do not refactor, rename, reformat,
  or add features.
- If an error says a member does not exist, the CONTRACT is correct and the call
  site is wrong - change the call to use what the contract actually declares.
  Never add the missing member to the contract type.
- If an error says a type, name, or namespace could not be found, add the
  correct import from the namespace map. If the name is not in the map, remove
  the reference rather than inventing an import.
- If an error says a type is defined more than once, this file is the duplicate:
  remove the redeclaration and import the canonical type instead.
- If an error points at a placeholder written as an expression or interpolation,
  replace it with a plain literal string, or better, read the value from the
  declared config object.
- Output the complete corrected file as raw content. No fences, no prose, no
  explanation of the fix.
"""

# Phase prompts are editable deployment configuration. The in-module text above remains
# as readable historical context, while these fail-closed loads are the runtime source.
from services.generation_governance import load_prompt_template as _load_generation_prompt
CONTRACT_PHASE_PROMPT = _load_generation_prompt("contract-phase")
MANIFEST_VALIDATION_PROMPT = _load_generation_prompt("manifest-validation")
REPAIR_PROMPT = _load_generation_prompt("repair")

try:
    _TIMEOUT = max(1.0, float(os.getenv("OLLAMA_TIMEOUT", "360")))
except ValueError:
    _TIMEOUT = 360.0
_configured_inference_width = (
    os.getenv("OLLAMA_MAX_CONCURRENT_REQUESTS")
    or os.getenv("MODERNIZATION_ASYNC_BATCH_WORKERS")
)
try:
    _OLLAMA_MAX_CONCURRENT_REQUESTS = (
        max(1, min(8, int(_configured_inference_width)))
        if _configured_inference_width else 0
    )
except ValueError:
    _OLLAMA_MAX_CONCURRENT_REQUESTS = 1
if not _OLLAMA_MAX_CONCURRENT_REQUESTS:
    from services.llm_runtime import inference_lanes as _runtime_inference_lanes
    _OLLAMA_MAX_CONCURRENT_REQUESTS = _runtime_inference_lanes()
_OLLAMA_INFERENCE_SLOTS = threading.BoundedSemaphore(_OLLAMA_MAX_CONCURRENT_REQUESTS)


def configure_inference_slots(lanes: int) -> int:
    """Apply an idle-time runtime profile to subsequent inference requests."""
    global _OLLAMA_INFERENCE_SLOTS, _OLLAMA_MAX_CONCURRENT_REQUESTS
    lanes = max(1, min(8, int(lanes)))
    _OLLAMA_MAX_CONCURRENT_REQUESTS = lanes
    _OLLAMA_INFERENCE_SLOTS = threading.BoundedSemaphore(lanes)
    return lanes


# ─── Public API ───────────────────────────────────────────────────────────────

# Function: check_status
def _generation_models(models: list[str]) -> list[str]:
    """Exclude embedding-only tags from the selectable generation runtime."""
    embedding_markers = ("embed", "nomic", "bge-", "e5-")
    return [model for model in models if not any(marker in model.casefold() for marker in embedding_markers)]


def check_status() -> dict:
    """
    Check Ollama availability and return the best available code model.
    Returns dict with: available, models, active_model, recommended, error
    """
    try:
        r = _httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=5)  # type: ignore[union-attr]
        r.raise_for_status()
        installed_models: list[str] = [m["name"] for m in r.json().get("models", [])]
        models = _generation_models(installed_models)
        active = _pick_model(models, CODEGEN_PREFERRED_MODELS)
        from services.llm_runtime import profiles_for_model, runtime_status
        runtime = runtime_status(models, active)
        active = runtime["selected_model"]
        return {
            "available":    True,
            "models":       models,
            "active_model": active,
            "recommended":  CODEGEN_PREFERRED_MODELS[0],
            "runtime": runtime,
            "model_profiles": {model: profiles_for_model(model) for model in models},
            "installed_models": installed_models,
        }
    except Exception as exc:
        return {
            "available":    False,
            "error":        str(exc),
            "models":       [],
            "active_model": None,
            "recommended":  CODEGEN_PREFERRED_MODELS[0],
        }


# Function: _resolve_model
def _resolve_model() -> str:
    status = check_status()
    if not status["available"]:
        raise RuntimeError(
            f"Ollama is not reachable at {OLLAMA_BASE}.  Start it and pull a model:\n"
            f"  ollama pull {DEVSTRAL_MODEL}"
        )
    model = status["active_model"]
    if not model:
        raise RuntimeError(
            f"Ollama at {OLLAMA_BASE} has no matching model installed. Pull one:\n"
            f"  ollama pull {DEVSTRAL_MODEL}"
        )
    return model


# A local Ollama instance under sustained multi-file generation load
# occasionally returns a bare 500 (model still swapping in, momentarily out
# of VRAM at a large num_ctx, brief internal overload) that clears up on its
# own moments later. Previously any single one of these — on any one file,
# anywhere in a multi-dozen-file project — propagated all the way up and
# aborted the ENTIRE generation run, discarding every file already produced.
# A short bounded retry absorbs exactly that transient class of failure
# without masking a real problem: a genuinely unavailable Ollama (connection
# refused) or a bad request (400/404 — wrong/uninstalled model) still fails
# immediately, since those are not in _TRANSIENT_STATUS_CODES / the transient
# exception tuple below.
_TRANSIENT_RETRY_ATTEMPTS = 3
_TRANSIENT_RETRY_BASE_DELAY = 2  # seconds; linear backoff: 2s, 4s
_TRANSIENT_STATUS_CODES = {500, 502, 503, 504}

_MAX_CONTINUATIONS = max(0, min(6, int(os.getenv("OLLAMA_MAX_CONTINUATIONS", "3"))))
_MIN_CONTINUATION_TOKENS = 64
_CONTINUATION_OVERLAP_CHARS = 2000
_DEFAULT_CONTEXT_WINDOW = max(4096, int(os.getenv("OLLAMA_CONTEXT_WINDOW", "32768")))
_DEFAULT_MAX_OUTPUT_CAP = max(2048, int(os.getenv("OLLAMA_MAX_OUTPUT_CAP", "24576")))


class GenerationTruncatedError(RuntimeError):
    """A streamed response hit its token ceiling; partial text is never final."""

    def __init__(self, partial_text: str):
        super().__init__(
            "LLM output was truncated at the configured token limit; "
            "no partial artifact was accepted"
        )
        self.partial_text = partial_text
        self.finish_reason = "length"


def count_tokens(value) -> int:
    """Count code-model tokens without contacting an external service.

    An optional deployment-owned local tokenizer can be configured; otherwise
    use the conservative three-characters-per-token estimate established by
    the generation pipeline.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    tokenizer_path = (os.getenv("MODERNIZATION_TOKENIZER_PATH") or "").strip()
    if tokenizer_path:
        try:
            from transformers import AutoTokenizer  # type: ignore
            tokenizer = getattr(count_tokens, "_tokenizer", None)
            tokenizer_key = getattr(count_tokens, "_tokenizer_path", None)
            if tokenizer is None or tokenizer_key != tokenizer_path:
                tokenizer = AutoTokenizer.from_pretrained(
                    tokenizer_path, local_files_only=True, trust_remote_code=False,
                )
                count_tokens._tokenizer = tokenizer
                count_tokens._tokenizer_path = tokenizer_path
            return max(1, len(tokenizer.encode(text, add_special_tokens=False)))
        except Exception as exc:
            logger.warning("Configured local tokenizer unavailable; using conservative estimate: %s", exc)
    return max(1, (len(text) + 2) // 3)


def model_context_window(model: str | None = None, requested: int = 0) -> int:
    """Return the bounded Ollama context window used by generation planning."""
    return max(4096, requested, _DEFAULT_CONTEXT_WINDOW)


def model_max_output_cap(_model: str | None = None) -> int:
    """Return the deployment-owned maximum new-token allowance."""
    return min(_DEFAULT_MAX_OUTPUT_CAP, model_context_window(_model) - 512)


def _stitch_generation_chunks(previous: str, continuation: str) -> str:
    """Join continuation text while removing repeated seam content."""
    # Java continuations occasionally ignore the requested overlap and restart
    # the complete compilation unit at its package declaration. Appending that
    # restart produces `...;package ...` in the middle of a method. Prefer the
    # restarted unit; subsequent continuation rounds can still finish it.
    previous_package = re.match(r"\s*(package\s+[A-Za-z_$][\w.$]*\s*;)", previous)
    continuation_package = re.match(r"\s*(package\s+[A-Za-z_$][\w.$]*\s*;)", continuation)
    if (
        previous_package and continuation_package
        and previous_package.group(1) == continuation_package.group(1)
    ):
        return continuation.lstrip()
    tail = previous[-_CONTINUATION_OVERLAP_CHARS:]
    best = 0
    for size in range(min(len(tail), len(continuation)), 7, -1):
        if tail.endswith(continuation[:size]):
            best = size
            break
    if best:
        return previous + continuation[best:]

    # Models do not always obey the instruction to begin the continuation at
    # the exact cut-off. They often restart the current SQL statement/member,
    # so the repeated suffix occurs *inside* the continuation. Joining only at
    # offset zero produced corrupt seams such as ``VALUES (9, 3INSERT INTO``.
    # Find the longest previous suffix repeated near the continuation start and
    # keep the already-streamed prefix while appending only what follows it.
    search_window = continuation[:4000]
    suffix_window = tail[-2000:]
    for size in range(min(len(suffix_window), len(search_window)), 11, -1):
        position = search_window.find(suffix_window[-size:])
        if position >= 0:
            return previous + continuation[position + size:]
    return previous + continuation


# Function: _is_transient_ollama_error
def _is_transient_ollama_error(exc: Exception) -> bool:
    if _httpx is None:
        return False
    if isinstance(exc, _httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS_CODES
    return isinstance(exc, (
        _httpx.ReadTimeout, _httpx.WriteTimeout, _httpx.PoolTimeout,
        _httpx.ConnectTimeout, _httpx.ConnectError, _httpx.RemoteProtocolError,
    ))


def _generation_timed_out(started: float, max_seconds: float | None) -> bool:
    return bool(max_seconds and time.monotonic() - started > max_seconds)


def _read_generation_response(
    response, started: float, max_seconds: float | None,
    on_token: Callable[[str], None] | None,
) -> str:
    accumulated: list[str] = []
    for line in response.iter_lines():
        if _generation_timed_out(started, max_seconds):
            raise TimeoutError(
                f"Ollama generation exceeded the {max_seconds:.0f}s per-file budget"
            )
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        token = data.get("response", "")
        accumulated.append(token)
        if on_token:
            # Surface reasoning activity without mixing it into generated code.
            on_token(token or data.get("thinking", ""))
        if data.get("done"):
            # Ollama reports a normal token-budget cutoff as a successful HTTP
            # response.  Returning the accumulated prefix here makes callers
            # indistinguishably treat a truncated source file as a complete
            # generation; Java then repeatedly surfaces EOF/dangling-try
            # diagnostics.  A length stop is not a usable artifact, so fail
            # the call before any caller can persist or cache the prefix.
            if str(data.get("done_reason") or "").casefold() == "length":
                raise GenerationTruncatedError("".join(accumulated))
            break
    return "".join(accumulated)


def _stream_generation_attempt(
    payload: dict, started: float, max_seconds: float | None,
    on_token: Callable[[str], None] | None,
) -> str:
    timeout = min(_TIMEOUT, max_seconds) if max_seconds else _TIMEOUT
    with _httpx.stream(  # type: ignore[union-attr]
        "POST", f"{OLLAMA_BASE}/api/generate", json=payload, timeout=timeout,
    ) as response:
        response.raise_for_status()
        return _read_generation_response(response, started, max_seconds, on_token)


# Function: _stream_generate_tokens
def _stream_generate_tokens(
    payload: dict, on_token: Callable[[str], None] | None,
    max_seconds: float | None = None,
) -> str:
    started = time.monotonic()
    for attempt in range(1, _TRANSIENT_RETRY_ATTEMPTS + 1):
        # `max_seconds` is meant to bound the ENTIRE call, retries included.
        # Without this check it only bounds each attempt individually (via
        # the httpx `timeout=` below) — an attempt that fails instantly,
        # every time, before a single line is ever read (so the per-line
        # check further down never runs) could otherwise still burn up to
        # `_TRANSIENT_RETRY_ATTEMPTS` full attempts before giving up.
        if _generation_timed_out(started, max_seconds):
            raise TimeoutError(
                f"Ollama generation exceeded the {max_seconds:.0f}s budget before attempt {attempt}"
            )
        try:
            return _stream_generation_attempt(payload, started, max_seconds, on_token)
        except GenerationTruncatedError:
            raise
        except Exception as exc:
            if attempt < _TRANSIENT_RETRY_ATTEMPTS and _is_transient_ollama_error(exc):
                logger.warning(
                    "Ollama generate transient error (attempt %d/%d), retrying: %s",
                    attempt, _TRANSIENT_RETRY_ATTEMPTS, exc,
                )
                retry_callback = getattr(on_token, "on_retry", None)
                if callable(retry_callback):
                    retry_callback(attempt + 1, _TRANSIENT_RETRY_ATTEMPTS, str(exc))
                time.sleep(_TRANSIENT_RETRY_BASE_DELAY * attempt)
                continue
            logger.exception("Ollama generate error: %s", exc)
            raise RuntimeError(f"LLM generation failed: {exc}") from exc
    raise RuntimeError("LLM generation failed after retries")  # unreachable


# Function: generate
def generate(
    prompt: str,
    model: str | None = None,
    system: str | None = None,
    on_token: Callable[[str], None] | None = None,
    max_tokens: int = 4096,
    num_ctx: int = 16384,
    think: bool | None = False,
    max_seconds: float | None = None,
    stop: Sequence[str] | None = None,
) -> str:
    """
    Generate text via Ollama.  Streams tokens internally.
    If on_token is provided it is called for each streamed chunk.
    Returns the full generated text.
    """
    if _httpx is None:
        raise RuntimeError("httpx is required: pip install httpx")

    if model is None:
        model = _resolve_model()

    payload: dict = {
        "model":      model,
        "prompt":     prompt,
        "stream":     True,
        "keep_alive": "10m",   # keep model hot between files — avoids 10-30 s reload
        "options": {
            "num_predict":    max_tokens,
            "num_ctx":        num_ctx,
            "num_thread":     -1,    # use all CPU cores for non-GPU layers
            "temperature":    0.10,  # low temp → deterministic, correct code
            "top_p":          0.90,  # matches SYSTEM_PROMPT's validated sampling (0.1-0.2 temp, 0.9 top_p)
            "top_k":          40,
            "repeat_penalty": 1.05, # mild penalty to discourage repetition
        },
    }
    if system:
        payload["system"] = system
    if stop:
        payload["options"]["stop"] = [str(value) for value in stop if str(value)]
    # Reasoning-capable models can consume the complete num_predict budget in
    # Ollama's separate `thinking` field and return an empty `response`. Code
    # generation needs the requested artifact, so keep reasoning explicitly
    # disabled unless a caller deliberately opts in.
    payload["think"] = bool(think)

    # Gate every language/framework through the same bounded semaphore. The
    # deployment-wide async batch width defaults to two and can be reduced to
    # one on constrained GPUs or raised (up to eight here) on larger hosts.
    started = time.monotonic()
    wait_budget = max_seconds if max_seconds else _TIMEOUT
    inference_slots = _OLLAMA_INFERENCE_SLOTS
    if not inference_slots.acquire(timeout=max(1.0, wait_budget)):
        raise TimeoutError(
            f"Timed out waiting for an Ollama inference slot after {wait_budget:.0f}s"
        )
    try:
        remaining = max_seconds
        if max_seconds:
            remaining = max_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError(
                    f"Ollama generation exceeded the {max_seconds:.0f}s budget waiting for an inference slot"
                )
        combined = ""
        active_payload = payload
        continuations = 0
        while True:
            if max_seconds:
                remaining = max_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError(
                        f"Ollama generation exceeded the {max_seconds:.0f}s aggregate budget"
                    )
            try:
                chunk = _stream_generate_tokens(active_payload, on_token, remaining)
                combined = _stitch_generation_chunks(combined, chunk) if combined else chunk
                return _strip_code_fences(combined)
            except GenerationTruncatedError as exc:
                combined = (
                    _stitch_generation_chunks(combined, exc.partial_text)
                    if combined else exc.partial_text
                )
                if continuations >= _MAX_CONTINUATIONS:
                    raise GenerationTruncatedError(combined) from exc

                context_window = model_context_window(model, num_ctx)
                contract_excerpt = prompt[:6000]
                if len(prompt) > 9000:
                    contract_excerpt += "\n...\n" + prompt[-3000:]
                tail = combined[-8000:]
                continuation_prompt = (
                    "Continue the exact source artifact below from its cut-off point. "
                    "Do not restart the file, do not add markdown, and do not explain. "
                    "Begin by repeating the final 120-300 characters exactly so the caller "
                    "can deduplicate the seam, then continue with only the missing source. "
                    "Close every open construct and emit every completion sentinel required "
                    "by the original contract.\n\n"
                    f"ORIGINAL CONTRACT (bounded excerpt):\n{contract_excerpt}\n\n"
                    f"CUT-OFF OUTPUT TAIL:\n{tail}"
                )
                prompt_tokens = count_tokens(continuation_prompt) + count_tokens(system or "")
                available = min(
                    model_max_output_cap(model),
                    context_window - prompt_tokens - 512,
                )
                if available < _MIN_CONTINUATION_TOKENS:
                    raise GenerationTruncatedError(combined) from exc
                continuation_tokens = min(
                    available,
                    max(4096, min(model_max_output_cap(model), max_tokens)),
                )
                active_payload = {
                    **payload,
                    "prompt": continuation_prompt,
                    "options": {
                        **payload["options"],
                        "num_predict": continuation_tokens,
                        "num_ctx": context_window,
                    },
                }
                continuations += 1
    finally:
        inference_slots.release()


# Function: _strip_code_fences
def _strip_code_fences(text: str) -> str:
    """Local code models may wrap
    output in Markdown code fences despite every system/task prompt in this
    codebase explicitly saying not to — the fenced text then gets written
    literally into a .cs/.ts/.json file as its first line, which is a hard
    syntax error before the file is evaluated on any other axis. Centralized
    here (rather than at each of the dozens of call sites across modernizer.py)
    so every caller gets clean output automatically.
    """
    if not text:
        return text
    t = text.strip()
    # Prefer an explicitly fenced source block wherever it appears. Local models
    # occasionally adds a short introduction before the opening fence or an
    # explanation after the closing fence even when asked for raw source.
    fenced = re.search(
        r"```[\w+\-]*\s*\r?\n(.*?)\r?\n```",
        t,
        re.DOTALL,
    )
    if fenced:
        return fenced.group(1)
    # Whole file fenced: ```lang\n...content...\n```
    m = re.match(r"^```[\w+\-]*\r?\n(.*?)\r?\n```\s*$", t, re.DOTALL)
    if m:
        return m.group(1)
    # Opening fence with no matching close — the closing ``` got cut off by
    # max_tokens truncation, or the model never emitted one.
    m = re.match(r"^```[\w+\-]*\r?\n(.*)$", t, re.DOTALL)
    if m:
        return re.sub(r"\r?\n```\s*$", "", m.group(1))
    # A model can omit the opening marker while still emitting a closing
    # marker followed by prose. Everything after that standalone marker is
    # non-source output and must never reach a compiler or artifact.
    closing = re.search(r"(?m)^\s*```\s*$", t)
    if closing:
        return t[:closing.start()].rstrip()
    return text


# ─── Internal helpers ─────────────────────────────────────────────────────────

# Function: _pick_model
def _pick_model(available: list[str], preferred: list[str] = PREFERRED_MODELS) -> str | None:
    """Return the highest-preference model that is actually available.

    Matches on the exact "name:tag" string. A substring match on just the
    base name (e.g. "qwen2.5-coder") would match every installed tag of that
    family at once — 3b, 7b, and 14b all contain that same base — silently
    picking whichever tag Ollama happens to list first instead of the tier
    actually requested.

    Falls back to whatever IS installed (rather than a preferred name that
    was never verified to exist) when none of `preferred` match —
    requesting an uninstalled model returns a 404 from Ollama's /api/generate
    on every call, which previously happened silently on every file.
    """
    for pref in preferred:
        if pref in available:
            return pref
    return available[0] if available else None


# Function: pick_codegen_model
def pick_codegen_model() -> str | None:
    """Best available model for actual code-generation calls.

    Uses the same Devstral-first policy reported by check_status(),
    falling back only when the configured default is not installed.
    """
    try:
        r = _httpx.get(f"{OLLAMA_BASE}/api/tags", timeout=5)  # type: ignore[union-attr]
        r.raise_for_status()
        available: list[str] = _generation_models([m["name"] for m in r.json().get("models", [])])
    except Exception:
        logger.warning("Code-generation model discovery failed for %s", OLLAMA_BASE, exc_info=True)
        return None
    from services.llm_runtime import requested_model
    requested = requested_model()
    if requested and requested in available:
        return requested
    return _pick_model(available, CODEGEN_PREFERRED_MODELS)


def pick_fast_codegen_model(fallback: str | None = None) -> str | None:
    """Use the top-level runtime selection for simple-file generation."""
    status = check_status()
    runtime = status.get("runtime") or {}
    return runtime.get("selected_model") or status.get("active_model") or fallback


# Function: pick_compiler_repair_model
def pick_compiler_repair_model(fallback: str | None = None) -> str | None:
    """Use the top-level runtime selection for compiler-guided rewrites."""
    status = check_status()
    runtime = status.get("runtime") or {}
    return runtime.get("selected_model") or status.get("active_model") or fallback
