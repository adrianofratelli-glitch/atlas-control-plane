# Mapa do código — atlas-control-plane

> Também conhecida como: Torre (control plane Atlas + assistente com MCP). Portas: frontend 5290 / backend 8765. Em uma frase: um painel de operação para clusters MongoDB Atlas (índices, consultas lentas, saúde, escala, FinOps) com um assistente de IA que consulta o cluster por MCP e só altera algo depois de uma aprovação explícita num cartão.

Os caminhos abaixo são relativos à raiz do repositório. Complementos: [architecture](briefing/architecture.md), [queries](briefing/queries.md), [ui-flows](briefing/ui-flows.md), [agent-behavior](briefing/agent-behavior.md) e [assistant.md](assistant.md).

## 1. Árvore anotada

```
backend/                       todo o servidor Python (FastAPI)
  api.py                       app e rotas /api/*: config, clusters, alertas, Advisor, slow queries, health, escala,
                               FinOps, explain, criar índice, relatório, conversas; monta o router do assistente
  assistant_api.py             router /api/assistant: stream NDJSON do assistente e cartões de aprovação
  assistant_runtime.py         um turno do assistente: abre o servidor MCP, lista ferramentas, roda o loop, traça no Langfuse
  assistant_graph.py           o loop modelo -> ferramentas em LangGraph (call_model / call_tools); checkpoint no Mongo
  assistant_tools.py           catálogo das 29 ferramentas (leitura e escrita), validação e execução (ClusterTools)
  assistant_actions.py         ActionStore: aprovações pendentes em SQLite local (.assistant-state/actions.sqlite3)
  assistant_report.py          modo "relatório": coleta evidências em paralelo e pede o texto ao modelo
  torre_mcp_server.py          servidor MCP (stdio) só-leitura; sobe com `python -m backend.torre_mcp_server PROJECT CLUSTER`
  atlas_client.py              cliente da Atlas Admin API (clusters, métricas, Advisor, slow queries, alertas, fatura, escala)
  cluster_identity.py          qual cluster o MONGODB_URI aponta, por host (o hash SRV é do projeto) — ADR-001
  cluster_insights.py          resumo estatístico de todos os nós (CPU média, p95, desequilíbrio)
  chat_memory.py               histórico de conversas no MongoDB da própria Torre (coleção chat_history, índices, TTL)
  ai_agent.py                  geração do relatório em PDF/Markdown e contagem de uso (helpers legados de chat foram trocados pelo MCP)
  llm_gateway.py               único caminho ao LLM: Grove (via _shared) e escolha do modelo
  tracing.py                   Langfuse opcional, com máscara de PII antes de criar o trace
  observability.py             logs e métricas; metrics_cache.py: cache curto de métricas
frontend/                      React + Vite (5290, proxy /api -> 8765)
  src/App.jsx                  casca, menu lateral e lista de páginas
  src/api.js                   TODAS as chamadas HTTP do front (axios e fetch para streams)
  src/cluster-context.jsx      cluster selecionado, compartilhado entre páginas
  src/assistant-stream.js      leitor do stream NDJSON do assistente
  src/pages/                   uma página por aba: Overview, PerformanceAdvisor, Profiler, Health, Scale, FinOps, Chat
  src/components/QueryDetails.jsx   painel "mostrar a query" usado pelas páginas
scripts/                       utilitários de linha de comando
  reset_demo.py                zera o que a Torre grava (histórico, checkpoints, aprovações, carga marcada)
  populate_workload.py         gera carga de escrita/leitura no banco de demo (só com ALLOW_DEMO_DB_WRITE=1)
  populate_profiler.py         gera consultas variadas para encher o Profiler e o Advisor
  stress_readonly.py           carga só de leitura, limitada em tempo e workers
tests/                         unittest offline (Python) e um teste de browser (browser-assistant.cjs)
docs/                          assistant.md e briefing/ (arquitetura, queries, fluxos de UI, comportamento do agente)
run_react.sh                   sobe API + UI (cria venv, instala dependências, faz build); `./run_react.sh`
docker/, Dockerfile, nginx.conf   empacotamento
```

## 2. "Apertei o botão X, o que roda?"

Seletor de cluster e pílulas de configuração ficam em `frontend/src/App.jsx` e `cluster-context.jsx`. As páginas são listadas em `frontend/src/App.jsx:18-24`.

| Tela / botão | Componente (arquivo:linha) | Chamada HTTP | Handler (arquivo:linha) | Função de negócio | Onde toca o MongoDB / Atlas |
|---|---|---|---|---|---|
| Abrir o app (config + lista de clusters) | `frontend/src/App.jsx:36` | `GET /api/config`, `GET /api/clusters` | `backend/api.py:263` `config`; `api.py:279` `list_clusters` -> `_list_clusters_sync` (286) | `AtlasClient.get_projects` (`atlas_client.py:158`), `get_clusters` (193) | Atlas Admin API `/groups`, `/groups/{id}/clusters`; nenhum acesso Mongo |
| Visão Geral: alertas e fatura | `frontend/src/pages/Overview.jsx:16`; `FinOps.jsx:36` | `GET /api/alerts`, `GET /api/invoice` | `api.py:330` `alerts`; `api.py:342` `invoice` | `get_open_alerts` (`atlas_client.py:571`), `get_pending_invoice` (581) | Atlas Admin API `/alerts`, `/invoices/pending` |
| Índices: "Buscar Recomendações" | `frontend/src/pages/PerformanceAdvisor.jsx:52` -> `load` (21), `getPA` | `GET /api/cluster/{project}/{cluster}/pa` | `api.py:361` `perf_advisor` | `AtlasClient.get_suggested_indexes` (`atlas_client.py:556`) | Atlas Admin API `performanceAdvisor/suggestedIndexes` |
| Índices: "Executar Índice" | `PerformanceAdvisor.jsx:77` -> `runIndex` (36), `createIndex` | `POST /api/index` | `api.py:756` `create_index` (valida em `_assert_namespace_is_safe` 655, `_assert_index_keys_are_safe` 667, `_assert_uri_targets` 624) | `atlas_client.py::create_index_direct` (600) | `create_index(keys)` no cluster observado (`atlas_client.py:640`), via `MONGODB_URI` |
| Índices: "Analisar com IA" e exportar relatório | `PerformanceAdvisor.jsx:91` -> `analyze` (28), `streamAnalyze`; exportar `PerformanceAdvisor.jsx:95` | `POST /api/analyze`; `POST /api/report` | `api.py:798` `analyze`; `api.py:893` `report` | `_mcp_text_response` (`api.py:777`) -> `assistant_api.py::assistant` (modo `report`) -> `assistant_report.py::run_report`; PDF: `ai_agent.py::generate_pdf_report` (51) | via ferramentas MCP (ver Assistente); histórico não é gravado no modo report |
| Consultas: "Carregar Slow Queries" | `frontend/src/pages/Profiler.jsx:97` -> `load` (34) | `GET /api/cluster/{project}/{cluster}/slow` | `api.py:372` `slow_queries` | `AtlasClient.get_slow_queries` (`atlas_client.py:561`) | Atlas Admin API `performanceAdvisor/slowQueryLogs` |
| Consultas: "plano" / explain de uma query | `Profiler.jsx:156,187` -> `runExplain` (70) | `POST /api/explain` | `api.py:724` `explain_query` | `explain` direto (`api.py:745`) | `command("explain", {find..., maxTimeMS})` no cluster observado, via `_mongo` (`api.py:253`) |
| Saúde: "Calcular Health Score" | `frontend/src/pages/Health.jsx:27` (`getHealth` em 18) | `GET /api/cluster/{project}/{cluster}/health` | `api.py:406` `health` | usa `get_primary`, `get_suggested_indexes`, `get_slow_queries`, `get_measurements` | Atlas Admin API (métricas, Advisor, slow log) |
| Escala: abrir a página (recomendação e métricas ao vivo) | `frontend/src/pages/Scale.jsx:31,38` | `GET .../live`, `GET .../scaling` | `api.py:574` `live_metrics`; `api.py:554` `scaling` | `get_cluster_processes`, `get_measurements`; `AtlasClient.recommend_scaling` (`atlas_client.py:459`), `estimate_cost` (590) | Atlas Admin API |
| Escala: "Executar Scaling" -> "Confirmar" | `Scale.jsx:166,168` -> `scaleCluster` (70) | `POST /api/cluster/{project}/{cluster}/scale` | `api.py:595` `scale` | `AtlasClient.scale_cluster` (`atlas_client.py:204`) | Atlas Admin API (PATCH no cluster); muda o tier de verdade |
| FinOps: "Atualizar métricas" | `frontend/src/pages/FinOps.jsx:81` -> `load` (28) | `GET /api/finops` | `api.py:503` `finops` -> `_finops_sync` (509) | `estimate_cost`, `cluster_insights`, `_tier_down` | Atlas Admin API |
| FinOps: "Analisar com MCP + LLM" | `FinOps.jsx:108` -> `analyze` (40), `streamAssistant` | `POST /api/assistant` (stream NDJSON) | `assistant_api.py:66` `assistant` | `assistant_runtime.py::run_assistant` (85) | ver Assistente |
| Assistente: enviar mensagem (ENTER, sugestões) | `frontend/src/pages/Chat.jsx:104` `send` (form em 206; sugestões em 200); `streamAssistant` em `api.js:97` | `POST /api/assistant` (NDJSON) | `backend/assistant_api.py:66` `assistant`; grava a pergunta em `save_user` (49) | `assistant_runtime.py::run_assistant` (85) -> `_produce` (32) abre MCP por stdio -> `assistant_graph.py::run_loop` (190): `n_call_model` (49) <-> `n_call_tools` (108) | `chat_memory.new_conversation` (`chat_memory.py:117`) e `add_message` (121) na coleção `chat_history`; checkpoints do LangGraph (`assistant_graph.py:170`); ferramentas MCP leem o cluster observado (tabela abaixo) |
| Assistente: aprovar / cancelar um cartão de operação | `Chat.jsx:64,65` -> `decide` (138) | `POST /api/assistant/actions/{id}` | `assistant_api.py:124` `decide` | `assistant_actions.py::ActionStore.claim` (67) / `finish` (80); `assistant_tools.py::ClusterTools.execute` (289) | SQLite local para o estado; escrita real no cluster observado (insert/update/delete, índices, coleções, search indexes, `collMod`) ou `scale_cluster` na Atlas API |
| Assistente: lista de operações da sessão | `Chat.jsx:92,100` | `POST /api/assistant/actions/list` | `assistant_api.py:119` `actions` | `ActionStore.recent` (84) | SQLite local |
| Assistente: ferramentas disponíveis | `Chat.jsx:84` | `GET /api/assistant/capabilities` | `assistant_api.py:43` `capabilities` | `TOOLS` em `assistant_tools.py:17` | nenhum |
| Assistente: histórico, abrir conversa, "Nova conversa", "Apagar conversa" | `Chat.jsx:93` (`listConversations`), `153` `openConversation`, `196`, `165` `removeConversation` | `GET /api/chat/conversations?q=`, `GET /api/chat/conversations/{id}`, `DELETE ...` | `api.py:913`, `927`, `943` | `chat_memory.py::list_conversations` (160), `search_conversations` (186), `load_messages` (149), `delete_conversation` (201) | `chat_history`: `aggregate`, `find` + `$text`, `find_one`, `delete_one` |
| Assistente: "Interromper" | `Chat.jsx:209` | cancela o fetch (abort) | `assistant_api.py` encerra o stream | — | — |
| Chat legado e análise em texto simples | não usados pelas páginas atuais (há `streamChat`/`streamAnalyze` em `api.js:66,69`) | `POST /api/chat`; `POST /api/analyze` | `api.py:877` `chat`; `api.py:798` | ambos chamam `_mcp_text_response` (`api.py:777`), o mesmo runtime MCP | idem Assistente |
| Saúde e métricas da própria Torre | — | `GET /api/health`, `/health/live`, `/api/metrics`, `/metrics` | `api.py:222,245,210,217` | `observability.metrics` | — |

**Ferramentas do assistente (MCP, `backend/torre_mcp_server.py`)**: o servidor expõe só as ferramentas de leitura (`list_tools` em `torre_mcp_server.py:35`, todas marcadas `readOnlyHint`); `call_tool` (42) chama `ClusterTools.call` (`assistant_tools.py:166`). Ferramentas de escrita (`write=True`) só preparam um cartão (`prepare`, `assistant_tools.py:254`, via `assistant_graph.py:136` `store.create`); a execução só acontece pelo botão "aprovar".

## 3. Onde estão as queries MongoDB

Detalhes: [docs/briefing/queries.md](briefing/queries.md). Há dois "bancos" diferentes: o da própria Torre (`MONGODB_URI`/`MONGODB_DB`, coleção `chat_history` e checkpoints) e o cluster observado (alvo do assistente e das ações da UI).

| O que faz | arquivo::função (linha) | Operador / pipeline | Coleção |
|---|---|---|---|
| Criar índices do histórico (idempotente) | `backend/chat_memory.py::init_db` (64): `create_index` em 72 (`text_search`, português), 80 (`updated_at_desc`), 84 (`cluster_idx`), 90 (`cluster_updated_at_idx`), 98 (TTL `updated_at_ttl`) | índices texto/simples/composto/TTL | `chat_history` |
| Criar conversa | `chat_memory.py::new_conversation` (117) | `insert_one` | `chat_history` |
| Anexar mensagem e título | `chat_memory.py::add_message` (138, 143) | `update_one` com `$push` + `$slice`; título condicionado | `chat_history` |
| Ler conversa | `chat_memory.py::load_messages` (151) | `find_one` com projeção | `chat_history` |
| Listar conversas | `chat_memory.py::list_conversations` (aggregate em 182) | `$match` (cluster), `$project` com `$size`, `$sort`, `$limit` | `chat_history` |
| Buscar conversas | `chat_memory.py::search_conversations` (192) | `find` com `$text` e `textScore` | `chat_history` |
| Apagar conversa | `chat_memory.py::delete_conversation` (203) | `delete_one` | `chat_history` |
| Checkpoint do loop do assistente | `backend/assistant_graph.py::_build_graph` (158; `MongoDBSaver` em 170); sem `MONGODB_URI` usa `MemorySaver` | escrita do LangGraph | `langgraph_checkpoints`, `langgraph_checkpoint_writes` |
| Explain da UI (aba Consultas) | `backend/api.py::explain_query` (724) | comando `explain` de `find` (filtro, sort e limit capturados), `maxTimeMS`; devolve o comando enviado | cluster observado |
| Criar índice pela UI (aba Índices) | `backend/atlas_client.py::create_index_direct` (640) | `create_index` | cluster observado |
| Assistente: listar bancos, coleções | `assistant_tools.py::ClusterTools.read` (214, 216) | `list_database_names`, `list_collection_names` | cluster observado |
| Assistente: amostrar campos (schema) | `ClusterTools.read` (222) | `find({}).limit(25)` | cluster observado |
| Assistente: índices e search indexes | `ClusterTools.read` (233, 235) | `list_indexes`, `list_search_indexes` | cluster observado |
| Assistente: contar, consultar, agregar, explicar | `ClusterTools.read` (237 `count_documents`, 245 `find` paginado, 248 `aggregate` até 30 estágios sem `$out`/`$merge`, 242 `explain` com `executionStats`) | validados por `validate_pipeline` (102), `validate` (122); `$regex` recusado fora do explain | cluster observado |
| Assistente: escritas aprovadas | `ClusterTools.prepare` (254; congela até 101 `_id` com `find`, 270) e `execute` (289): `insert`, `update`, `delete`, `create_index`, `drop_index`, create/drop collection, `collMod` (validator), create/update/drop search index | só após aprovação no cartão | cluster observado |
| Reset da demo | `scripts/reset_demo.py::reset` (64 `drop_collection` de `chat_history`, 68 checkpoints, 69 `delete_many` da carga marcada) | `drop_collection`, `delete_many` | `chat_history`, checkpoints, `eventos_pix` marcado |
| Cargas de demonstração | `scripts/populate_workload.py` (`find`, `aggregate`, `count_documents`, escrita com `insert_many` em 181 e `update_many` em 183), `scripts/populate_profiler.py`, `scripts/stress_readonly.py` | consultas variadas, inclusive prefixos com regex de propósito (exceção documentada: precisam ser lentas para o Profiler) | banco de demo do cluster observado |

Atlas Admin API (HTTP, não é MongoDB): todas em `backend/atlas_client.py` (`_get` 137, `_patch` 146): grupos/clusters (158-218), processos e medidas (218-460), Advisor e slow queries (556-570), alertas (571), fatura (581).

## 4. Compartilhado × só desta PoV

**Vem de `_shared` (pacote `pov-shared`, instalado com `uv pip install --python venv/bin/python -e "../_shared[llm]"`):**
- `grove_client` (`AsyncGroveClient`) -> chamadas ao LLM: `backend/llm_gateway.py::async_client` (43; import na 46: `from grove_client import AsyncGroveClient`). Sem fallback para chave Anthropic.
- `guardrails.mask_pii` -> máscara de PII antes do Langfuse: `backend/tracing.py:18`.
- Nenhum outro módulo de `_shared` é importado em `backend/`.

**Variáveis de ambiente:**
- Comuns (do `.env.shared` via `sync_env`): `GROVE_BASE_URL`, `GROVE_API_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `USD_BRL` (tarifa), `MONGODB_URI` quando aponta para o cluster da demo.
- Locais (só desta PoV): `ATLAS_PUBLIC_KEY`, `ATLAS_PRIVATE_KEY`, `ATLAS_ORG_ID`, `ATLAS_PROJECT_ID` (acesso à Admin API), `MONGODB_DB` (banco do histórico), `CLAUDE_MODEL`, `API_AUTH_TOKEN`, `API_HOST`, `CHAT_RETENTION_DAYS`, `CHAT_MAX_MESSAGES`, `TORRE_ACTION_DB` (caminho do SQLite de aprovações), `LOG_JSON`, `LOG_LEVEL`, `ALLOW_DEMO_DB_WRITE`, portas `API_PORT`/`WEB_PORT` (em `run_react.sh` e `frontend/vite.config.js`), `TORRE_STRESS`, `STRESS_MINUTES`, `STRESS_WORKERS`.

**Bancos/coleções:** `chat_history`, `langgraph_checkpoints`, `langgraph_checkpoint_writes` são da Torre (banco `MONGODB_DB`). O cluster e o banco de demo observados (dados como `eventos_pix` e o dataset pré-existente) são entradas externas que a Torre lê e, só com aprovação, altera; não são exclusivos desta PoV.

**Chamadas externas:**
- LLM (Claude via Grove): `llm_gateway.py` (`async_client`, `resolve_model`) usado em `assistant_graph.py::n_call_model` (49) e `assistant_report.py::run_report` (14).
- MCP: o runtime lança o servidor como subprocesso: `assistant_runtime.py` (`StdioServerParameters(... "-m", "backend.torre_mcp_server" ...)`, em `_produce`, 32).
- Atlas Admin API: `backend/atlas_client.py` (credenciais por env; sem host/chave neste mapa).
- Langfuse (fail-open, trace criado depois da máscara): `backend/tracing.py::_get_client` (86) e uso em `assistant_runtime.py`.
- Voyage: não usada nesta PoV (não encontrado).

## 5. "Quero mexer em..." (atalhos)

- Texto/rótulo de uma aba ou botão -> o arquivo da página em `frontend/src/pages/` (ex.: `PerformanceAdvisor.jsx:52`); nomes do menu em `frontend/src/App.jsx:18`.
- Adicionar uma rota -> `backend/api.py` (padrão `@app.get/post`) e a função em `frontend/src/api.js`.
- Prompt do assistente -> `SYSTEM` em `backend/assistant_runtime.py:16`; perguntas iniciais do chat -> `STARTERS` em `frontend/src/pages/Chat.jsx`.
- Adicionar ou ajustar uma ferramenta do assistente -> `backend/assistant_tools.py` (`tool(...)` em 24-52, execução em `ClusterTools.read` 172 / `prepare` 254 / `execute` 289).
- Regras do que o assistente pode consultar (limites, `$out`, `$regex`) -> `validate_pipeline` (102) e `ClusterTools.validate` (122) em `assistant_tools.py`.
- Mudar o que exige aprovação -> campo `write` em `tool(...)` e `assistant_actions.py`.
- Regra de recomendação de escala -> `AtlasClient.recommend_scaling` em `backend/atlas_client.py:459`.
- Regra de FinOps (quando sugere descer de tier) -> `backend/api.py::_finops_sync` (509) e `backend/cluster_insights.py`.
- Índices ou retenção do histórico -> `backend/chat_memory.py::init_db` (64), `CHAT_RETENTION_DAYS`.
- Modelo do LLM -> `CLAUDE_MODEL` ou `DEFAULT_MODEL` em `backend/llm_gateway.py:18`.
- Mudar porta -> `API_PORT`/`WEB_PORT` em `run_react.sh` (linhas 23-24) e `frontend/vite.config.js`.
- Mudar a carga/seed da demo -> `scripts/populate_workload.py`, `scripts/populate_profiler.py`; reset em `scripts/reset_demo.py`.

## 6. Testes

- Python (offline, Atlas/Grove/MCP/Langfuse falsos): `venv/bin/python -m unittest discover -s tests -v` (comando do README).
- `tests/test_api_guards.py` — validações de namespace, chaves e filtros de `api.py`.
- `tests/test_assistant.py` — ferramentas, validação e cartões de aprovação do assistente.
- `tests/test_assistant_graph.py` — loop de ferramentas (`assistant_graph.py`).
- `tests/test_atlas_client.py` — `recommend_scaling` (função pura).
- `tests/test_cluster_insights.py` — resumo estatístico dos nós.
- `tests/test_chat_memory.py` — validação de `conversation_id`.
- `tests/test_reports_and_cache.py` — relatórios e cache de métricas.
- `tests/test_client_concurrency.py` — concorrência na criação do cliente.
- `tests/test_bootstrap_docs_and_slow_log.py` — docs de bootstrap coerentes com o código e slow log.
- `tests/test_hardening_adversarial.py` — suíte adversarial (LLM só via Grove, entradas hostis, MCP/Langfuse falsos).
- Front: `frontend/tests/assistant-stream.test.mjs`, `stream-lifecycle.test.mjs` (leitor do stream NDJSON), rodam com `node --test frontend/tests`; `package.json` não tem script `test`. Navegador: `tests/browser-assistant.cjs` (Playwright, exige o app no ar).
