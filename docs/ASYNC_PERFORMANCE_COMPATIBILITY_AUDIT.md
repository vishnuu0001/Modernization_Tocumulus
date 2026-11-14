# Asynchronous execution and compatibility audit

## Scope and result

The standalone service accepts analysis and modernization requests through job APIs. Those APIs return a job ID and execute CPU, compiler, and model work in a bounded background `ThreadPoolExecutor`; status, progress events, and output are persisted so a service restart does not turn a known job into a 404. Async here means requests do not hold the browser/API request open while generation runs. The Ollama HTTP stream itself is blocking and occupies a worker thread, which is appropriate for this synchronous client and does not block the ASGI event loop.

This change applies a bounded asynchronous batch width across all supported stacks, aligns Ollama request admission with that width, applies configured model timeouts, bounds queued jobs and per-file domain inference, removes duplicate C# inference, and adds cooperative cancellation. All technology targets use the same orchestration and inference gate. Framework support still depends on each scaffold and installed validator/compiler; it cannot truthfully mean every historical and future version.

## Changes

- Limit top-level generation workers to one by default and admit at most four queued jobs. Both values can be changed with `MODERNIZATION_JOB_WORKERS` and `MODERNIZATION_JOB_QUEUE_CAPACITY`. Requests beyond capacity fail with a retryable capacity message instead of accumulating in an unbounded executor queue.
- Default all file-generation stages to `MODERNIZATION_ASYNC_BATCH_WORKERS=2`. Planned-file generation, domain generation, source conversion, closure, validation, module-boundary repair, and compiler repair inherit the same policy for every supported language; phase and Java-specific overrides remain capped at four.
- Admit two Ollama requests by default with `OLLAMA_MAX_CONCURRENT_REQUESTS=2` and configure the bundled server with `OLLAMA_NUM_PARALLEL=2`. Operators can reduce the shared width to one on constrained GPUs or increase explicit Ollama admission up to eight on larger hosts.
- Apply one source-grounded file-conversion attempt and a 600-second aggregate per-file ceiling to every target language by default (`MODERNIZATION_CONVERSION_MAX_ATTEMPTS`, `MODERNIZATION_CONVERSION_FILE_MAX_SECONDS`). Keep compiler/build repair as the cross-file quality gate, stream filename/token heartbeats, publish each completed conversion immediately, and bound every language's complete conversion wave.
- Bound Java strict-prebuild repair to 600 seconds total (`MODERNIZATION_JAVA_PREBUILD_REPAIR_TOTAL_BUDGET_SECONDS`), schedule only up to the configured worker count instead of queueing the whole diagnostic set, stream repair-token heartbeats, publish each result immediately, and stop queued repairs at the shared deadline. Non-source SQL/YAML assets are excluded from the Java source-repair batch, and deterministic Java reconciliation runs before LLM repair.
- Honor `OLLAMA_TIMEOUT` in the model client (default 360 seconds when unset; deployment config supplies 600 seconds).
- Job deletion now requests cooperative cancellation for running jobs and cancels queued jobs. Running model/compiler operations may finish their current blocking step before cancellation is observed at the next progress/file boundary.
- C# domain source authored and syntax-validated by the endpoint/service/repository specialists is excluded from the later general source pass. The previous flow sent those same files through a second LLM generation, duplicating expensive inference and risking weaker replacements.
- Count base generation calls, not repair attempts, toward domain progress. Emit a bounded job heartbeat every 64 generated tokens so long model calls remain visibly active when their percentage does not change.
- Bound C# and TypeScript domain draft plus repair calls by `MODERNIZATION_FILE_GENERATION_MAX_SECONDS` (default 1800 seconds). Stop Java/C# repair early when compiler diagnostics are identical after an attempt.
- Keep job checkpoints on the persistent application data volume and recover interrupted jobs on startup, preserving their last progress and making them visibly failed/interrupted rather than returning 404.
- Include .NET SDK 8 alongside 9 and 10. Install Java LTS JDK/JRE 8, 11, 17, and 21 side by side and select the project's declared Java release for Maven/Gradle builds.
- Install Node.js 20 and 22 side by side. Select Node 20 for Angular 17/18 and Node 22 for Angular 19–22 and React 16–19, matching the official Angular compatibility table.
- Install PostgreSQL, MariaDB/MySQL-compatible, SQL Server ODBC 18/sqlcmd, and SQLite CLI clients. Keep PostgreSQL 16 as the only embedded Compose server; use local SQLFluff/SQLGlot parsers for vendor/cloud dialect checks and external endpoints for other database engines.
- Generate Angular scaffolds for majors 17–22 and React scaffolds for majors 16–19, selecting compatible TypeScript/dependency and React entry-point conventions. Unsupported requested majors fail explicitly instead of silently receiving an unrelated fallback. Target presets are provided for these ranges.

## Compatibility boundaries

| Technology | Image/scaffold support | Qualification boundary |
|---|---|---|
| .NET | SDK/runtime majors 8.0, 9.0, 10.0 | Side by side in the image; targets outside these majors fail preflight. Project dependencies still restore from NuGet. |
| Java | JDK/JRE majors 8, 11, 17, 21 | Each supported LTS runtime/compiler is installed. Maven and Gradle builds select the matching JDK from project release metadata. Java 9/10/12–16/18–20 are outside the bounded set. |
| Node.js | 20.x and 22.x | Both include npm/npx. Node 20 is selected for Angular 17/18; Node 22 for Angular 19–22 and React 16–19. |
| Angular | Scaffold majors 17–22 | Project-local CLI/compiler versions are resolved by npm; TypeScript is pinned to the Angular-compatible minor. Framework packages are not installed globally. |
| React | Scaffold majors 16–19 | Project-local packages are resolved by npm. React 16/17 use `ReactDOM.render`; 18/19 use `createRoot`. |
| Database clients | PostgreSQL client, MySQL-compatible/MariaDB client, SQL Server ODBC 18 + `sqlcmd`/`bcp`, SQLite 3 CLI | PostgreSQL 16 is the only database server in standalone Compose. SQL Server, MySQL, Oracle, Db2, MongoDB and cloud database engines are external endpoints; SQLFluff/SQLGlot provide local dialect parsing where supported. Drivers restore with the generated project. |
| Other catalogued stacks | Shared job queue, cancellation checkpoints, Ollama gate, and progress persistence | Build validation varies by installed compiler/parser and is reported through toolchain readiness. A catalog entry alone does not guarantee strict compilation for every variant. |

“All versions” is not a finite or verifiable compatibility promise. The exact majors and supported dialects above are the bounded targets in this image. To add a release outside them, add its target metadata, compatible scaffold/dependency pins, required SDK/runtime, and an applicable build validator.

## Performance limits

The default batch width is two. Reduce `MODERNIZATION_ASYNC_BATCH_WORKERS`, `OLLAMA_MAX_CONCURRENT_REQUESTS`, and `OLLAMA_NUM_PARALLEL` together to one if GPU memory pressure or throughput measurements regress; raise them only after confirming the model server can sustain more simultaneous contexts. CPU/RAM and temporary project size still set practical limits. Cancellation is cooperative because Python cannot safely terminate an in-flight thread or HTTP request.

## Deployment verification record

The optimized image `modernization-studio-standalone:1.0.12` is deployed (Linux/amd64, image ID `sha256:e35d73a9d6401128fc1e67c84493156fdc43bba6fb46c4af7363a350074c26ec`). The app container is healthy and `/api/health` returns HTTP 200 with `status: ok`.

Runtime inventory inside the deployed image reports .NET SDKs 8.0.427, 9.0.320, and 10.0.403; Java/JDK 8.0.504, 11.0.32, 17.0.20, and 21.0.12; Node.js 20.20.2/npm 10.8.2 and Node.js 22.23.4/npm 10.9.9. The `psql`, `mysql`, `sqlcmd`, and `sqlite3` clients are executable; SQLFluff and SQLGlot report 17 SQL dialects. Compatibility checks accept the listed .NET, Java, Angular 17–22, and React 16–19 targets, reject Java 22, Angular 23, React 20, and require Java 17+ for Spring Boot 3.

The app-only Compose rollout left the existing PostgreSQL and Ollama containers healthy with restart count zero. PostgreSQL data and the application volume were preserved. Database clients and SQL parsers do not imply that corresponding database servers are bundled; standalone Compose runs PostgreSQL 16 only, and other engines must be provided through external endpoints.

The 24B model was observed generating at 2.4–2.8 tokens/second on an RTX 4070 SUPER, with Ollama reporting 85% GPU / 15% CPU placement and 11,507 MiB of 12,282 MiB GPU memory in use. This explains the remaining hardware throughput limit; inference remains serial to avoid adding queued requests or VRAM contention. Fully GPU-resident 24B execution needs more VRAM or a smaller model. The previously active 86% job was marked interrupted by the app-only rollout; its source snapshot and persistent data remain, but generated files had not been checkpointed into the job output.
