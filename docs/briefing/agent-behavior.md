# Torre — Atlas Control Plane — Comportamento do Assistente (agente de IA)

> Cobre o **Assistente operacional** (aba "Assistente" / `POST /api/assistant`),
> que é o componente com "ai-chat" mais sofisticado da PoV. O loop de tool-use
> do Claude (via gateway Grove, `llm_gateway.py`) sobre o servidor MCP próprio roda hoje como um StateGraph do
> LangGraph (`assistant_graph.py`, migrado 2026-09-29) — dois nós,
> `call_model`/`call_tools`, que se alternam até faltar tool_use ou estourar um
> dos dois tetos. `assistant_runtime.py` continua dono da sessão MCP, do
> client do gateway (`AsyncGroveClient`) e do trace Langfuse (ligados ao `async with` do transporte
> stdio), e só delega a alternância pensar/agir pro grafo.
>
> As rotas `/api/chat` e `/api/analyze` também usam esse runtime MCP real. Relatórios (`mode=report`) coletam evidências por MCP e fazem uma única chamada de síntese; o chat mantém o loop de ferramentas. `/api/report` apenas renderiza PDF.

## 1. Visão geral do fluxo

```
React Chat.jsx
  --fetch NDJSON--> POST /api/assistant (assistant_api.py)
      --> assistant_runtime._produce(...)
            spawna um subprocesso MCP por rodada:
              torre_mcp_server.py PROJECT_ID CLUSTER_NAME
            --> loop de até 12 iterações de tool-use (Claude via Grove)
                  --> ClientSession MCP (stdio) --> ClusterTools (assistant_tools.py)
      --> eventos NDJSON: connected, trace_url, text, tool_start, tool_end, action, done/error
  --cartão de aprovação no chat--> POST /api/assistant/actions/{id}
      --> assistant_actions.store.claim(...) (SQLite, reivindicação atômica)
      --> ClusterTools.execute(...) (única gravação real)
```

## 2. "Nodes" — as etapas reais do loop (`assistant_runtime._produce` + `assistant_graph.py`)

O loop reason-act é um StateGraph real de dois nós (`assistant_graph.py`):
`call_model` (chama o Claude em streaming, decide se pede ferramenta) e
`call_tools` (executa as chamadas MCP, trata a prévia de escrita). Arestas
condicionais nos dois sentidos: `call_model` vai pra `call_tools` só se pediu
ferramenta, `call_tools` volta pra `call_model` só se não estourou o teto de
40 chamadas — sem essa segunda aresta condicional, o teto de tool calls não
impedia uma rodada extra do modelo (bug real, achado escrevendo o teste que
não existia antes da migração). Checkpoint em MongoDB via
`langgraph-checkpoint-mongodb` quando `MONGODB_URI` está configurada;
`MemorySaver` em processo quando não. As etapas, em ordem:

1. **Setup do turno** (`assistant_runtime.py:33-53`) — sobe um `StdioServerParameters`
   apontando pro mesmo interpretador Python rodando
   `torre_mcp_server.py PROJECT_ID CLUSTER_NAME` como subprocesso, com env
   restrito a `PATH, HOME, LANG, ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY,
   ATLAS_ORG_ID, ATLAS_PROJECT_ID, MONGODB_URI` (nada mais do ambiente do host
   vaza pro child). O histórico é recortado às últimas 16 mensagens e
   truncado até começar com uma mensagem de `user` (`assistant_runtime.py:40-42`).
2. **Carrega o registro de ações recentes** da sessão via
   `store.recent(session_id, project_id, cluster_name)` (SQLite) e injeta no
   `system prompt` como JSON (`operation`, `state`, `result` de cada ação —
   `assistant_runtime.py:43-46`). É assim que o modelo sabe se uma ação que ele
   preparou já foi aprovada, cancelada ou expirou, sem precisar de uma
   ferramenta de consulta separada.
3. **Abre trace Langfuse** (`tracing.start_trace`, fail-open — sem chave, vira
   no-op) e emite o evento `trace_url` assim que disponível, antes mesmo da
   resposta terminar (`assistant_runtime.py:56-71`).
4. **Conecta ao servidor MCP** via stdio (`stdio_client` + `ClientSession`,
   `assistant_runtime.py:63-67`), lista as ferramentas expostas e filtra pelo
   catálogo local `TOOLS` (`assistant_tools.py`) — dupla checagem entre o que o
   servidor anuncia e o que o cliente reconhece.
5. **Loop de tool-use** (até 12 iterações, `assistant_runtime.py:73-142`):
   - Chama `client.messages.stream(...)` com `system`, `history` e as `tools`
     do MCP, streamando texto token a token via `emit({"type": "text", ...})`.
   - Registra uma *generation* no Langfuse por iteração
     (`tracing.log_generation`, com tokens de input/output/cache).
   - Se a resposta não pede nenhuma ferramenta, encerra com `{"type": "done"}`
     (ou avisa truncamento em `max_tokens`).
   - Se pede ferramentas, para cada `tool_use`: emite `tool_start`, chama
     `mcp.call_tool(name, input)`, decodifica o resultado JSON, registra um
     *span* Langfuse por chamada (`tracing.log_span`), emite `tool_end`.
   - **Limite de 40 chamadas de ferramenta por rodada** (contador `calls`,
     `assistant_runtime.py:107-111`) — corta a análise em vez de deixar o
     modelo martelar ferramentas indefinidamente.
6. **Encerramento**: fecha o cliente do gateway se foi criado aqui, e
   sempre finaliza a trace Langfuse com o texto completo da resposta (mascarado)
   (`finally`, `assistant_runtime.py:144-147`).

`run_assistant` (`assistant_runtime.py:150-174`) é só o adaptador que
transforma o produtor (`_produce`, que fala via `emit`/callback) num gerador
assíncrono consumido pela rota FastAPI, com cancelamento limpo do subprocesso
MCP se o cliente HTTP desconectar.

## 3. Estado — checkpoint do grafo + o que já existia

O checkpointer do LangGraph (`assistant_graph.py`, `MongoDBSaver`/`MemorySaver`
por turno, `thread_id=session_id`) persiste o estado do StateGraph a cada
super-step — é o que sobrevive a um crash no meio do loop, hoje. Além dele, o
"estado" do agente é composto de três coisas com durabilidade e escopo
diferentes, sem mudança pela migração:

| Estado | Onde vive | Durabilidade | Escopo |
|---|---|---|---|
| Histórico de mensagens da rodada (`history`) | Em memória, dentro de `_produce` | Só a rodada atual (últimas 16 mensagens vindas do cliente) | Uma chamada a `/api/assistant` |
| Conversa completa (mensagens exibidas na UI) | `torre.chat_history` via `chat_memory.py` (mesma coleção usada pelo chat simples) | Persistente, TTL de 90 dias | Por conversa |
| Ações propostas/aprovadas/executadas | SQLite (`.assistant-state/actions.sqlite3`, `assistant_actions.py`) | Persistente, retenção de 7 dias, até 20 pendentes por sessão | Por sessão + projeto + cluster |

Não existe "memória de longo prazo" do agente em si (nada de embeddings/RAG
sobre conversas passadas alimentando o próximo turno) — o contexto que o
modelo recebe é: histórico recente da conversa + registro de ações da sessão
(`outcomes`, montado a cada turno em `assistant_runtime.py:44`).

## 4. Ferramentas — o "tool belt" do agente

29 ferramentas, geradas dinamicamente a partir do catálogo declarativo `TOOLS`
em `assistant_tools.py:24-51` e servidas por `torre_mcp_server.py`, que **não
expõe nenhuma ferramenta de execução real** — só leitura e preparação
(`ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
openWorldHint=False)`, `torre_mcp_server.py:36-38`). Catálogo completo,
schemas e regras de validação estão em `queries.md`, seção 3.

Resumo por área (do `docs/assistant.md`, confirmado no código):

| Área | Leitura | Alteração (com aprovação) |
|---|---|---|
| Atlas | configuração, métricas 5min, séries 24h, alertas do projeto, custo estimado | tier do cluster (`atlas_scale`) |
| Performance | todas as páginas do Performance Advisor, slow queries, índices existentes, explain | criar/remover índice |
| Dados | bancos, coleções, amostra de campos/tipos, find, count, aggregate | inserir/atualizar/excluir documentos |
| Coleções | descobrir estrutura observada | criar/excluir coleção, definir validator |
| Search/Vector | listar índices e estados, agregações com `$search`/`$vectorSearch` | criar/atualizar/remover índices |

Deliberadamente **fora do catálogo**: shell, MQL arbitrário com efeito
colateral, administração de usuários/IAM, exclusão de banco, backup/restore.
O `SYSTEM` prompt (`assistant_runtime.py:17-26`) instrui o modelo a descrever
esses limites honestamente em vez de fingir suporte.

## 5. "Memória" no sentido de contexto do modelo — e o que ela NÃO é

- `chat_memory.py` guarda a **conversa exibida**, não um cache semântico nem
  memória vetorial. Não há prompt caching real nem economia de token
  mensurável no loop do chat — por decisão explícita do projeto,
  **não force um card de "economia de
  tokens"** aqui: seria inventar uma história que os dados não sustentam.
- O registro de ações (`assistant_actions.store`) funciona como uma memória de
  curto prazo *operacional* (o que já foi proposto/decidido nesta sessão),
  não como memória conversacional.
- Dados de documentos, nomes e resultados de ferramentas são tratados
  explicitamente no `SYSTEM` prompt como **conteúdo não confiável**: o modelo
  é instruído a nunca executar uma ação porque ela "apareceu" dentro de um
  documento lido via ferramenta (`assistant_runtime.py:22`) — mitigação de
  prompt injection via dado do próprio cluster.

## 6. O fluxo de aprovação (onde a execução real acontece)

Nenhuma ferramenta MCP grava no banco. O fluxo de duas fases:

1. **Preflight** (`ClusterTools.prepare`, `assistant_tools.py:218-251`) —
   roda dentro do próprio `mcp.call_tool`, no mesmo turno em que o modelo
   pediu a ferramenta de escrita. Só lê (ex.: conta quantos documentos
   casam o filtro, congela os IDs). Devolve `{"proposal": True, "operation",
   "arguments", "details", "frozen_ids", "destructive"}`.
2. `assistant_runtime._produce` detecta `data.get("proposal") is True` e
   **antes de devolver o resultado ao modelo**, grava a ação no SQLite
   (`store.create(...)`), emite o evento `{"type": "action", ...}` (o cartão
   que a UI renderiza), e substitui o conteúdo devolvido ao modelo por
   `{"status": "pending_user_approval", ...}` — o modelo nunca recebe um ID de
   aprovação como argumento de ferramenta, então não pode "aprovar a si
   mesmo" em outra chamada (`assistant_runtime.py:124-130`).
3. Humano clica **Aprovar e executar** na UI → `POST
   /api/assistant/actions/{id}` (`assistant_api.py`) → `store.claim(...)`
   reivindica o token com `BEGIN IMMEDIATE` e transição de estado atômica
   (`assistant_actions.py:67-78`) → só então `ClusterTools.execute(...)`
   roda a gravação de verdade.
4. Estados possíveis de uma ação: `pending` (aguardando aprovação, 15 min de
   validade), `executing`, `completed`, `submitted` (operações assíncronas
   como mudança de tier/Search — precisam ser consultadas até concluir),
   `cancelled`, `expired`, `unknown` (falha após início — pode haver alteração
   parcial; a mesma aprovação nunca é reenviada).

## 7. Guardas específicas do agente (resumo — detalhes em `queries.md`)

- Todo pipeline de agregação passado pelo modelo é validado contra uma lista
  fechada de estágios de leitura antes de rodar (`assistant_tools.READ_STAGES`).
- Filtros/updates/validators são varridos recursivamente contra operadores
  que rodam JS/expressão arbitrária (`$where`, `$function`, `$accumulator`,
  além de `$out`/`$merge` bloqueados no pipeline).
- Namespace vindo do modelo passa pela mesma gramática usada na criação de
  índice via UI (`api._assert_namespace_is_safe`).
- `mongo_update`/`mongo_delete` exigem filtro não vazio, e travam em até 100
  documentos com IDs congelados na prévia.
- Limite de 20 aprovações pendentes por sessão; histórico de 20 itens
  recentes, retenção de 7 dias.
- Rodada inteira: até 12 iterações do loop, 40 chamadas de ferramenta, tempo
  de execução ainda limitado pelo timeout HTTP do cliente.

## 8. Observability do agente (Langfuse)

`tracing.py` — fail-open: sem `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` no
`.env`, todas as chamadas viram no-op e o turno segue normal. Um `auth_check()`
roda uma vez por processo para nunca expor um link que dê 404 no meio de uma
demo. Por turno: uma trace (`torre.turn`), uma *generation* por iteração de
raciocínio do LLM, um *span* por chamada de ferramenta MCP. O evento
`trace_url` sai no stream logo após `connected`, e o `Chat.jsx` renderiza o
badge "Ver trace no Langfuse" assim que chega.

**Nunca mencionar Postgres em call/demo/material de cliente** — é infra
interna do Langfuse (self-host), invisível ao usuário, e citá-la mistura a
mensagem com um concorrente direto da MongoDB.

## 9. O chat simples (`ai_agent.py`) — para contraste

`/api/chat`, `/api/analyze` e `/api/report` **não** usam o loop de ferramentas
MCP. `ai_agent.py` monta o contexto (métricas, custo, status já buscados pelo
backend via `atlas_client.py`) e chama o Claude diretamente para três coisas:
análise textual, chat com streaming, geração de relatório PDF. Modelo default
Sonnet 5 (`CLAUDE_MODEL`). Histórico compartilha a mesma coleção
`torre.chat_history`. Esse caminho não tem ferramentas, não tem aprovação e
não gera ações — é só leitura + texto.

## Relatórios de FinOps — v3.1.0

`assistant_report.py` coleta `atlas_cluster_insights`, `atlas_cluster`, `atlas_indexes` e `atlas_slow_queries` na mesma sessão MCP. Se o tier estiver disponível, consulta `atlas_cost`. Cada leitura tem prazo de 45s; a resposta do modelo, 75s. Erros de coleta viram evidências indisponíveis, sem serem interpretados como zero. O runtime envia heartbeat a cada cinco segundos, e o frontend encerra o estado de espera com conclusão ou erro explícito.

O relatório não prepara alterações nem executa explains automaticamente. Distingue sugestões do Advisor de índices existentes e limita decisões de capacidade à cobertura disponível. O chat pode aprofundar a análise com `mongo_explain`, incluindo filtro, sort e limite.

## Endurecimento 3.2.0 (2026-10)

- **Gateway único, falha fechado.** `llm_gateway.async_client()` exige `GROVE_BASE_URL` e `GROVE_API_KEY` antes de abrir o MCP; sem eles o stream devolve um `error` explicando o que configurar e termina com `done`. Haiku é mapeado para `claude-sonnet-5-5`. Retry/backoff, circuit breaker e troca de chave vêm do `grove_client` (`_shared`), cobrindo a abertura do stream.
- **Trace depois da máscara.** `tracing.mask_for_trace` aplica `guardrails.mask_pii` do `_shared` (CPF, CNPJ, e-mail, telefone) mais cartão (Luhn) e connection strings a toda trace, generation e span, inclusive entradas/saídas de ferramenta. O modelo continua recebendo o texto original; só a observabilidade é mascarada.
- **Estado por turno.** O grafo recebe todos os canais (`tool_uses`, `outcome`) zerados a cada turno. Antes, com checkpoint, o `outcome` do turno anterior era restaurado e encerrava o turno 2+ logo após a chamada ao modelo, sem executar as ferramentas pedidas.
- **Banco interno fora do alcance.** O banco de estado (`MONGODB_DB`, padrão `torre`, com histórico e checkpoints de todas as sessões) é bloqueado nas ferramentas de dados e oculto em `mongo_databases`. `$regex` é recusado em find/count/aggregate (busca textual é `$search`); `explain` ainda aceita, para diagnosticar a slow query.
- **Prompt injection.** O catálogo não tem shell, usuários/papéis, `dropDatabase`, backup nem comando arbitrário. Pedidos destrutivos, inclusive vindos de documento ou "aprovação do diretor" em texto, só viram cartão `pending` com `destructive: true`; a execução depende do endpoint de aprovação, de uso único sob concorrência.

