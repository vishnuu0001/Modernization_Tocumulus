# Modernization Studio

Modernization Studio is a self-contained FastAPI and React module. It runs on
`http://127.0.0.1:18084/` and does not require the StratIQ portal or any sibling
application module.

The backend runtime requires Python 3.14; the qualified local and container
version is Python 3.14.10. Create `.venv` with that interpreter before installing
`requirements.txt` and `requirements-dev.txt`.

For the portable, source-free container image, cloud-agnostic deployment
options, security controls, checksums, and client handoff instructions, see
[`Delivery/README.md`](Delivery/README.md) and
[`Delivery/DEPLOYMENT_GUIDE.md`](Delivery/DEPLOYMENT_GUIDE.md).

The bundle includes the tested Linux image archive, complete-stack and
external-services Compose manifests, TLS ingress, and Windows/Linux secret
bootstrap scripts.

## Start

```powershell
.\Start-Modernization.ps1
```

The launcher builds the frontend, starts the backend, verifies the module-owned
health endpoint, and starts the automatic publisher. Frontend source changes
are rebuilt into `frontend/dist`; backend changes under `api/` or `services/`
are applied by a controlled restart. Backend publication is deferred while any
generation, requirements, or toolchain job is active, and it also defers when
job state cannot be verified. Use `-SkipBuild` for an unchanged frontend,
`-NoAutoPublish` to explicitly disable synchronization, or `-Port 8085` to
select another local port.

Runtime logs are written under `data\logs`, and the running process ID is
stored in `data\modernization.pid`.

## Development

Run the backend with `.\.venv\Scripts\python.exe -m api.server` and the UI
with `npm.cmd run dev` from `frontend`. Vite proxies `/api` to port 18084.

Standalone mode binds to loopback and supplies the local operator identity used
for audit metadata and administrator-only local operations. A network deployment
can set `AUTH_REQUIRED=true` and `AUTH_TOKEN_SECRET` to use the module's own
signed-token validation.

## Governed code generation and release qualification boundary

Every prompt-driven run now freezes the request before writing code. The generated
project contains a `governance/` directory with:

- `requirements-manifest.json` — immutable `IN-###`, `OUT-###`, entity, use-case,
  role, integration, NFR, stack/version, constraint, sensitivity, state, and assumption
  records. Its checksum detects any later mutation.
- `guidelines.json` — every supplied rule in its original wording, with `G-###`
  project IDs or `R-###` run IDs, scope, language, precedence, conflicts, and any
  derived machine check.
- `language-standards.json` and `file-plan.json` — only the versioned standards packs
  relevant to the run, plus the dependency-ordered implementation/test plan.
- `generation-audit.json`, `guideline-compliance.json`, and
  `input-output-traceability.json` — per-file accountability, conflict decisions,
  rule application/N-A reasons, and action-level capture/validation/persistence/rendering
  evidence for every manifest input and output.

Generation runs in five enforced phases: frozen requirements; frozen contracts and file
plan; dependency-ordered file generation; compile/lint/contract/guideline/placeholder
validation with bounded repair; and final audit reports. A file that is too large must
return a validated structured split request. Unsafe paths, partial output, missing notes,
unaccounted rules, missing traceability actions, placeholders, lint violations, or build
failures make `generation_validated=false`. Code generation always records
`production_ready=false`: a successful local source/build/test gate is not a production
readiness certification. Release approval additionally requires deployment-environment,
external-integration, security, operations, performance, resilience, and business evidence.

Files within the same dependency wave are generated asynchronously. Every supported language,
including Java, inherits `MODERNIZATION_ASYNC_BATCH_WORKERS=2`; tune the shared width or
`MODERNIZATION_FILE_WORKERS` and `MODERNIZATION_JAVA_FILE_WORKERS` from 1 to 4 for the
available Ollama hardware. Domain generation, source conversion, closure, validation, and
compiler repair use the same policy with optional phase-specific overrides. Per-file
calls receive the relevant manifest slice and rules, while the immutable on-disk manifest
retains the complete verbatim request. Deterministic dependency manifests and framework
configuration are generated locally instead of consuming LLM time.
Folder modernization uses one source-grounded conversion draft per file and a 600-second
aggregate ceiling by default across every target language. Configure these through
`MODERNIZATION_CONVERSION_MAX_ATTEMPTS` and
`MODERNIZATION_CONVERSION_FILE_MAX_SECONDS`; Java-specific overrides remain available.
Syntax and cross-file correctness are enforced afterward by strict validation and the
dependency-aware whole-project compiler/build repair stage.
Java strict-prebuild repair has a separate 600-second total ceiling configured with
`MODERNIZATION_JAVA_PREBUILD_REPAIR_TOTAL_BUDGET_SECONDS`. Repair files are scheduled
only as worker capacity becomes available, each completion is reported immediately,
and queued work is abandoned when the shared deadline is reached.
Planning uses a larger bounded budget for full-stack contracts and multi-module projects.
If Ollama reports an output-length cutoff, the planner retries exactly once with an expanded
bounded budget and a correspondingly sized context window; truncated plans are never persisted.
Planning prompts never receive the per-file source-envelope protocol. They terminate with a
planner-only `END_OF_PLAN` stop sequence enforced by the Ollama request, preventing a model from
continuing into source generation during the planning phase.

The same gate applies to every full-generation language in the target catalog. Editable
language standards are paired with `config/generation/language-acceptance.json`, which
declares the build/dependency manifests and runnable test artifacts required for each
language. Java uses Maven/Gradle verification, .NET builds and tests, npm workspaces build
and run their declared tests, Python restores dependencies into an isolated environment
and runs pytest/unittest, SQL parses every project script, and deployment/CI artifacts are
validated by their registered parsers. Missing toolchains, manifests, tests, parsers, or
requested framework capabilities fail the run; they are never converted into success.

COBOL batch requests that require copybooks, JCL, sample input/output, and control totals
use the deterministic governed batch pack. A run is accepted only after GnuCOBOL compiles
and links the program, the sample job exits successfully, and its report matches the golden
output byte-for-byte. The delivered pack includes the fixed-format program, copybooks, JCL,
sample data, expected report, run instructions, and the same governance reports.

### Supplying guidelines and instructions

On **Review & generate**, attach `.md`, `.txt`, `.docx`, text PDFs,
`.editorconfig`, and common formatter/linter configuration files. UTF-8 and UTF-16 text
are accepted without replacement decoding; unsupported, unreadable, scanned-only, or
oversized files fail with a clear request error. Attached machine configuration is copied
into the generated repository and participates in validation. Put run-specific constraints
in **Extra instructions**. The API accepts the same values as `files` and
`custom_stack_desc` on `/api/modernize/analyze-prompt`.

Precedence is deterministic: security/data-protection rules, client/project guidelines,
run instructions, frozen contracts/manifest, built-in language standards, then model
defaults. Client rules therefore override a conflicting run or language default. Every
conflict remains visible in `guidelines.json` and the generation audit; nothing is silently
discarded.

Editable prompts are in `config/generation/prompts/`. Versioned standards for every
registered language are in `config/generation/language-packs/`. Run the offline governance
and evaluation gates with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_generation_governance.py tests\test_generation_evaluation_cases.py -q
```
# Secure standalone startup

Modernization uses its own PostgreSQL-backed account and session store. Run the
one-time initializer from PowerShell, enter the local PostgreSQL administrator
password when prompted, and then start the module:

```powershell
.\Initialize-ModernizationDatabase.ps1
.\Start-Modernization.ps1
```

Sign in as `admin`. The initial password is supplied separately by the system
owner. Every new browser session must upload
`Modernization-Credential-2026-11-30.mcred`; its Ed25519 signature is checked
before access and it expires at the end of 30 November 2026 UTC. Startup and
active sessions use a quorum of independent HTTPS UTC sources and fail closed
if trusted time is unavailable, disagrees, rolls backward, or is expired. Sessions are
revoked after 30 minutes without user activity, on sign-out, and are not
retained after the browser session closes.

