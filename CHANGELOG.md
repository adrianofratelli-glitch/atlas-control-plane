# Changelog

## Unreleased

- Langfuse fora do ar (causa vista ao vivo em 2026-11-13: o self-host local não estava respondendo; chaves e host conferem com o projeto do self-host): em vez do `Unexpected error occurred` do SDK e do tracing desligado até reiniciar, a Torre faz um health check curto e o `auth_check` numa thread de fundo (o turno nunca espera a rede; client descartado é encerrado), registra **um** aviso por queda com a causa, conta os turnos sem trace e as linhas do SDK (`langfuse_*` em `/api/metrics`, mais `tracing.status`) e tenta de novo em silêncio a cada `LANGFUSE_RETRY_SECONDS` (300 s). Testes em `tests/test_tracing_failopen.py`.
- Explain fiel (aba Consultas): `POST /api/explain` aceita o `sort` e o `limit` da consulta capturada, devolve o comando exato enviado (`command`) e procura o índice no plano inteiro; antes, `SORT → FETCH → IXSCAN` aparecia como "COLLSCAN (sem índice)". A tela mostra o nome do comando (`find`, `getMore`, `aggregate`) e só oferece explain para `find`: antes, um `getMore` rodava `find({})` na coleção e mostrava o plano de outra consulta. Testes em `tests/test_explain_fidelity.py`.
- Didática: cada tela tem o resumo "Demonstra no Atlas" com a capacidade demonstrada e as chamadas que o servidor faz (Admin API v2 e driver), conferido contra `backend/atlas_client.py` por `tests/test_atlas_capabilities.py`. O Assistente mostra o nome da ferramenta MCP em cada passo.
- Segurança (núcleo): a identidade do cluster-alvo das ações diretas (explain, criar índice, ferramentas `mongo_*` do assistente, execução de aprovações) comparava só o hash do host SRV, que é do **projeto**; com vários clusters no mesmo projeto, uma ação para o cluster B rodava no cluster do `MONGODB_URI`. Agora a comparação é por host (`backend/cluster_identity.py`, ADR-001); `is_uri_target` marca um único cluster. Testes em `tests/test_cluster_identity.py`.
- Health Score: status e versão vêm só do Atlas no servidor (a query string é ignorada; Admin API fora ou sem estado/versão = sem nota, com o motivo na resposta e na página Saúde); antes, uma chamada sem parâmetros dava "Status (em transição)" e "MongoDB 0" para um cluster IDLE. Texto de versão atualizado (8.0+; 9.0 é a mais recente).
- Eval: `eval/run_assistant_live.py` + `eval/golden_assistant.json` (rascunho, 6 casos) medem as respostas do assistente ao vivo com regras determinísticas; antes do fix de identidade 4/6 (uma das falhas era falso positivo da regra, corrigido; a outra, o assistente listando bancos do cluster do `MONGODB_URI` com outro cluster selecionado), 6/6 depois.
- Estrutura: o código de runtime saiu da raiz para o pacote `backend/` (api, atlas_client, assistant_*, ai_agent, chat_memory, cluster_insights, llm_gateway, metrics_cache, observability, tracing, torre_mcp_server) e os utilitários de linha de comando (`populate_workload.py`, `populate_profiler.py`, `stress_readonly.py`) foram para `scripts/`. Imports passam a ser `backend.*`; o servidor sobe com `uvicorn backend.api:app` e o servidor MCP com `python -m backend.torre_mcp_server`. `run_react.sh`, `docker/start.sh`, Dockerfile, CI, testes e docs foram atualizados. Sem mudança de comportamento.
- Atualiza dependências compatíveis e pisos de segurança; alinha metadados de versão entre aplicação e frontend.

## 3.2.1 (2026-10-09)

- API: `/api/cluster/{project}/{cluster}/slow` asks Atlas for `nLogs` and caps the response (default 1000 lines, `?limit=` 1–5000) instead of returning the whole slow log (~12 MB on the demo cluster). The Profiler says how many entries it grouped.
- API: `/scaling` no longer answers 422 when `tier` is omitted; it uses the cluster's current tier and returns it (`tier` capped at 32 chars).
- Docs: every `docker build` passes `--build-context shared=../_shared`; bootstrap only mentions the Grove gateway (no direct provider key). Regression tests in `tests/test_bootstrap_docs_and_slow_log.py`.
- Deps: `pov-shared` 0.2.1 (editable); its `mask_pii` now covers card numbers too, the local Luhn mask stays as defense in depth.

## 3.2.0 (2026-10-06)

- LLM: every Claude call goes through the Grove gateway via `pov-shared` (`backend/llm_gateway.py` → `grove_client.AsyncGroveClient`), with retry/backoff on 429/5xx, circuit breaker and key failover. The assistant fails closed without `GROVE_BASE_URL`/`GROVE_API_KEY`; the direct-provider fallback and the unused synchronous chat/analysis helpers were removed. Haiku maps to `claude-sonnet-5-5`.
- Tracing: CPF, CNPJ, e-mail, phone, card numbers and connection strings are masked in every Langfuse trace, generation and span; the turn trace is created after masking.
- Assistant: follow-up turns in the same session now run the tools the model requests (a stale checkpointed outcome ended them early). Torre's internal database (chat history and every session's checkpoints) is no longer readable through the data tools, and `$regex` is rejected outside `explain`.
- API: Atlas ids are validated before any Admin API call; GETs retry 502/503/504 and connection resets; Atlas 5xx/timeouts return a sanitized 502; `explain` is bounded by `maxTimeMS` and no longer echoes driver errors.
- Ops: `scripts/reset_demo.py` (idempotent, guarded by `ALLOW_DEMO_DB_WRITE`); `scripts/populate_workload.py` writes only with that flag and tags its documents; the `$regex` load generators are documented as the explicit exception.
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
