# Torre — Atlas Control Plane — Arquitetura

> Onde está o quê, e por quê. Consultas e índices MongoDB estão em `queries.md`,
> telas e fluxos em `ui-flows.md`, comportamento do agente de IA em `agent-behavior.md`.

## O problema que a PoV resolve

Quem opera várias organizações/projetos Atlas passa o dia navegando entre telas
da própria UI do Atlas pra responder pergunta simples: quais clusters estão
superdimensionados, o que o Performance Advisor está apontando, quanto está
custando e onde está o desperdício. O Torre consolida isso numa tela só e
permite perguntar em linguagem natural, com o modelo enxergando dado real da
Admin API — não um resumo pré-mastigado.

## Stack

| Camada | Escolha | Onde |
|---|---|---|
| Frontend | React 18 + Vite + LeafyGreen (design system MongoDB) | `frontend/src/` |
| Backend | FastAPI (Python) | `api.py` |
| Cluster observado | Atlas Admin API v2 (`HTTPDigestAuth`) | `atlas_client.py` |
| IA — análise/chat/PDF | Anthropic Claude, direto via SDK | `ai_agent.py` |
| IA — assistente operacional | Anthropic Claude + MCP (Model Context Protocol) sobre stdio | `assistant_runtime.py`, `torre_mcp_server.py`, `assistant_tools.py` |
| Memória do chat | MongoDB Atlas via `pymongo` | `chat_memory.py` |
| Aprovações do assistente | SQLite local, single-host | `assistant_actions.py` (`.assistant-state/actions.sqlite3`) |
| Observability opcional | Langfuse (self-host, fail-open) | `tracing.py` |
| Log/métricas em processo | logging estruturado + `GET /api/metrics` | `observability.py` |

## Componentes (módulos Python planos na raiz, sem pacote aninhado)

| Arquivo | Papel |
|---|---|
| `api.py` | backend FastAPI, todas as rotas `/api/...`, middleware de request-id e métricas, auth opcional por bearer token |
| `atlas_client.py` | cliente da Atlas Admin API v2 + heurística determinística de recomendação de escala |
| `ai_agent.py` | análise Claude (síntese de custo/performance/status), chat com streaming, geração de relatório PDF |
| `chat_memory.py` | histórico de chat persistido no Atlas via pymongo (ver `queries.md`) |
| `assistant_api.py` | rotas `/api/assistant/...` — conversa e aprovação/execução de ações |
| `assistant_runtime.py` | loop de ferramentas Anthropic ↔ MCP do assistente operacional (ver `agent-behavior.md`) |
| `assistant_tools.py` | catálogo das 28 ferramentas MCP, validação de entrada, leitura e preparação de escrita |
| `assistant_actions.py` | `ActionStore` — aprovações duráveis, single-use, em SQLite |
| `torre_mcp_server.py` | servidor MCP sobre stdio; expõe só leitura + preparação, nunca execução |
| `observability.py` | log estruturado (`LOG_JSON=1`) e métricas em processo |
| `tracing.py` | integração Langfuse fail-open |
| `populate_workload.py`, `populate_profiler.py` | seeds standalone que geram carga real (find/aggregate) pra Performance Advisor e Query Profiler não ficarem vazios na demo |

## Fluxo de dados

### Leitura de frota (Overview, Health, Scale, FinOps)

```
React (frontend/src/pages/*.jsx)
  --axios /api--> FastAPI (api.py)
      --> atlas_client.AtlasClient --> Atlas Admin API v2
      (heurística de escala roda em atlas_client.recommend_scaling, testada com unittest)
  <-- JSON --
```

### Chat simples (análise/relatório)

```
React Chat.jsx --fetch streaming--> /api/chat, /api/analyze, /api/report
  --> ai_agent.py --> Anthropic Claude (texto) + dados já buscados da Admin API
  --> chat_memory.py --> MongoDB Atlas (histórico)
```

### Assistente operacional (aba Assistente)

```
React --fetch NDJSON--> /api/assistant (assistant_api.py)
  --> assistant_runtime._produce (loop Anthropic tool-use)
      --> MCP client (stdio) --> torre_mcp_server.py --> assistant_tools.ClusterTools
              --> leitura: Atlas Admin API / pymongo
              --> escrita: só PREPARA (proposal), nunca executa
  --> aprovação do usuário no cartão --> POST /api/assistant/actions/{id}
      --> assistant_actions.store (SQLite) reivindica o token atomicamente
      --> ClusterTools.execute(...) --> Atlas Admin API (scaleCluster) / pymongo (única vez que grava)
```

Detalhado nó a nó em `agent-behavior.md`.

## Decisões de arquitetura (o "porquê")

- **O número vem da heurística determinística; o modelo escreve a justificativa.**
  A recomendação de escala (subir/descer/manter tier) sai de
  `AtlasClient.recommend_scaling` (`atlas_client.py:470-565`), pura, testada com
  `unittest`, sem LLM no caminho. O Claude recebe a recomendação pronta e
  traduz em argumento de negócio. Isso existe porque a pergunta que sempre vem
  é "esse tier foi o modelo que chutou?" — a resposta precisa ser "não, tem
  teste unitário cobrindo a regra", e a UI separa número e justificativa
  literalmente na diagramação (ver `ui-flows.md`).
- **Decisão de escala não pode sair de um instantâneo de 5 minutos.** Quando
  existe série de 24h, scale up olha p95 (pico importa pra subir) e scale down
  olha a média (comportamento típico importa pra descer) — assimétrico de
  propósito. Scale down também exige todos os sinais baixos ao mesmo tempo e
  nunca desce de M10.
- **Não inventa métrica.** Se a Admin API não devolve, a tela — e o assistente
  — dizem que não devolve, em vez de estimar. Ponto nulo é ponto nulo: pega o
  último valor não-nulo em vez de assumir zero (zero e "não reportou" são
  coisas diferentes; confundir as duas produz um scale-down bonito e errado).
- **Escopo de escrita minimizado e isolado.** Sobre a Admin API, o único ponto
  de escrita é `AtlasClient.scale_cluster` (`atlas_client.py:186-197`),
  chamado só depois de aprovação explícita — qualquer revisão de segurança tem
  um lugar único pra olhar. No assistente operacional, todas as 28 ferramentas
  MCP são somente-leitura ou apenas preparam uma proposta (`ClusterTools.prepare`,
  `assistant_tools.py:218-251`); a execução real fica isolada em
  `ClusterTools.execute` (`assistant_tools.py:253-307`) e só roda depois que
  `assistant_actions.store.claim(...)` reivindica o token de aprovação de forma
  atômica no SQLite.
- **Credencial nunca chega ao frontend.** `ATLAS_PUBLIC_KEY`/`ATLAS_PRIVATE_KEY`
  e `MONGODB_URI` vivem só no `.env` do backend. O browser só fala com `/api`.
- **Guardas de injeção nas duas superfícies onde entrada de usuário toca o
  banco:** `_oid()` valida `conversation_id` antes de virar `ObjectId`
  (`chat_memory.py:47-53`); namespace/chaves de índice passam por gramática
  explícita antes do driver (`api._assert_namespace_is_safe`,
  `api._assert_index_keys_are_safe`, usadas também por `assistant_tools.safe_namespace`).
  No assistente, todo pipeline de agregação é validado contra uma lista fechada
  de estágios de leitura (`assistant_tools.READ_STAGES`) e uma lista de
  operadores proibidos que executam JS/expressão arbitrária
  (`assistant_tools.safe_expression`, bloqueando `$where`, `$function`,
  `$accumulator`, `$out`, `$merge`).
- **Cache com TTL nas chamadas caras da Admin API.** Resolver o primário de um
  cluster custa uma sequência de chamadas; múltiplas telas pedindo isso a cada
  render transformaria a Admin API no gargalo da própria ferramenta
  (`atlas_client.py:82-115`, TTL de 60s pro primário e 30s pra listagens).
- **Métrica vem do primário, resolvido explicitamente** — não do primeiro
  processo que a lista devolver (`atlas_client.py:203-273`, usa o hash do
  cluster na `connectionString`, porque nomes genéricos de cluster colidem com
  "mongodb" no hostname de qualquer cluster).
- **Aprovações são duráveis e single-use**, guardadas em SQLite
  (`.assistant-state/actions.sqlite3`, fora do Git) e não no processo, pra
  sobreviver a restart do worker; reivindicadas com `BEGIN IMMEDIATE` +
  atualização de estado numa transação, pra dois cliques na mesma aprovação
  nunca executarem duas vezes.
- **Um único CLAUDE_MODEL, com fallback Sonnet 5.** O assistente operacional e
  a análise simples reusam o mesmo modelo/config, evitando duas fontes de
  verdade sobre "qual Claude está respondendo".

## Ambiente

| Variável | Obrigatória | Papel |
|---|---|---|
| `ATLAS_PUBLIC_KEY` / `ATLAS_PRIVATE_KEY` | sim | credenciais da Admin API |
| `ATLAS_ORG_ID` | sim | organização a inspecionar |
| `ANTHROPIC_API_KEY` | sim | assistente, análise e relatórios |
| `MONGODB_URI` | não | criação de índices direta + histórico de chat + dados do assistente |
| `CLAUDE_MODEL` | não | default Sonnet 5 |
| `API_AUTH_TOKEN` | não | liga autenticação bearer em todo endpoint `/api` não-health |
| `TORRE_ACTION_DB` | não | caminho alternativo do SQLite de aprovações |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | não | observability opcional, fail-open |

## Comandos

```bash
./run_react.sh                              # sobe backend (8765) + frontend (5290)
uvicorn api:app --reload --port 8765
cd frontend && npm run dev / npm run build
python -m unittest discover -s tests -v     # sem credencial Atlas/Mongo
python populate_workload.py                 # semeia carga pra Performance Advisor/Profiler
python populate_profiler.py
docker build -t torre . && docker run --env-file .env -p 18085:8080 torre
```
