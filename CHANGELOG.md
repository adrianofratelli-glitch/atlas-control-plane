# Changelog

## 3.2.0 (2026-10-06)

- LLM: every Claude call goes through the Grove gateway via `pov-shared` (`llm_gateway.py` → `grove_client.AsyncGroveClient`), with retry/backoff on 429/5xx, circuit breaker and key failover. The assistant fails closed without `GROVE_BASE_URL`/`GROVE_API_KEY`; the direct-provider fallback and the unused synchronous chat/analysis helpers were removed. Haiku maps to `claude-sonnet-5-5`.
- Tracing: CPF, CNPJ, e-mail, phone, card numbers and connection strings are masked in every Langfuse trace, generation and span; the turn trace is created after masking.
- Assistant: follow-up turns in the same session now run the tools the model requests (a stale checkpointed outcome ended them early). Torre's internal database (chat history and every session's checkpoints) is no longer readable through the data tools, and `$regex` is rejected outside `explain`.
- API: Atlas ids are validated before any Admin API call; GETs retry 502/503/504 and connection resets; Atlas 5xx/timeouts return a sanitized 502; `explain` is bounded by `maxTimeMS` and no longer echoes driver errors.
- Ops: `scripts/reset_demo.py` (idempotent, guarded by `ALLOW_DEMO_DB_WRITE`); `populate_workload.py` writes only with that flag and tags its documents; the `$regex` load generators are documented as the explicit exception.
- Launcher: one `./venv`, literal `.env` loading, `pov-shared` install, Vite reinstall when missing, loopback bind.
- UI: removed the unrouted Compare and Clusters pages; the assistant banner explains the gateway configuration. `axios` 1.20.0 (npm audit high).
- Tests: adversarial suite (`tests/test_hardening_adversarial.py`); tests never write to a real database.

## 3.1.0 (2026-10-02)

- UI: MongoDB Dark Stage v4, local fonts, shared dark tokens and responsive layouts.

- FinOps: exact cluster/process matching, CPU mean across replica-set nodes in common five-minute intervals, busiest-node p95 and historical coverage. Missing data and paused clusters no longer imply healthy utilization or zero billing.
- Capacity recommendations require adequate evidence before suggesting a lower tier; memory usage alone does not establish cache pressure.
- All UI analysis/chat routes obtain model evidence through the real MCP stdio runtime. FinOps reports use bounded evidence collection and a single model response, with progress, cancellation and explicit failures.
- Scaling polls every five seconds without overlapping requests. Live Atlas collections replace the one-minute response cache; historical charts were removed from the page. New metric values remain subject to Atlas sampling granularity.
- The launcher starts a bounded read-only workload by default, with configurable duration/workers, duplicate-run protection and process cleanup.
- PDF formatting, query explain sorting, slow-query pagination and stream completion were corrected. Added regression coverage for insights, reports and caching.

## 1.0.0 (2026-09-30)

First public release.

- Repository rebuilt with a clean, single-commit history.
- English README and repository description, with screenshots captured against a real Atlas cluster.
- MIT license.
- Internal notes, presentation decks, test-output snapshots, and tooling configuration removed from the repository.
