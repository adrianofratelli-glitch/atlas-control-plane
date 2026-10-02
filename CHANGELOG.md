# Changelog

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
