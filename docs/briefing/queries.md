# Torre — Atlas Control Plane — Queries, agregações e índices

> Inventário completo, extraído do código real (grep por `.aggregate(`, `.find(`,
> `create_index`, chamadas HTTP à Atlas Admin API). Caminho é `arquivo:linha`.
> Índices em `torre.chat_history` (memória do chat) e as queries direto no
> cluster observado (seeds, explain, índice via UI) são coisas diferentes —
> tratadas em seções separadas.

---

## 1. Memória do assistente — `torre.chat_history` (via pymongo)

Coleção própria da aplicação (não é o cluster de demo observado). Um
documento por conversa: `{cluster, project_id, title, messages[], created_at,
updated_at}`.

### 1.1 Índices — `chat_memory.init_db` (`chat_memory.py:64-102`)

`init_db()` é idempotente: lê `list_indexes()` e só cria o que falta, então
pode ser chamado a cada boot sem custo.

| Nome | Definição | Linha | Por que existe |
|---|---|---|---|
| `text_search` | `[("title", TEXT), ("messages.content", TEXT)]`, `default_language="portuguese"` | `chat_memory.py:72-76` | Busca full-text sobre título e conteúdo das mensagens, sem precisar de Elasticsearch. `default_language="portuguese"` porque as conversas são em pt-BR — stemming em inglês degradaria a busca em silêncio. |
| `updated_at_desc` | `[("updated_at", DESCENDING)]` | `chat_memory.py:80` | Suporte a listagem "mais recentes primeiro" sem filtro de cluster. |
| `cluster_idx` | `[("cluster", DESCENDING)]` | `chat_memory.py:84` | Filtro isolado por cluster (contexto da conversa). |
| `cluster_updated_at_idx` | `[("cluster", 1), ("updated_at", DESCENDING)]` | `chat_memory.py:89-93` | Índice composto que cobre a forma real da query de `list_conversations` (`$match` em `cluster` + `$sort` por `updated_at` desc) num índice só, em vez de dois separados. |
| `updated_at_ttl` | `[("updated_at", 1)]`, `expireAfterSeconds=CHAT_RETENTION_DAYS*86400` | `chat_memory.py:97-102` | TTL **sempre ativo**: histórico de demo esquecido vira custo pra sempre. `CHAT_RETENTION_DAYS` cai para 90 dias se ausente/vazio/≤0 — um valor "desligado" nunca significa "guardar para sempre" (correção da auditoria de resiliência de 2026-09). |

### 1.2 Queries — `chat_memory.py`

| Operação | Onde | O que faz | Exemplo |
|---|---|---|---|
| `insert_one` | `chat_memory.py:117` (`new_conversation`) | Cria o documento de conversa vazio. | `{"cluster": "demo-cluster", "project_id": "…", "title": "Nova Conversa", "messages": [], "created_at": ..., "updated_at": ...}` |
| `update_one` ($push + $slice) | `chat_memory.py:129-146` (`add_message`) | Anexa mensagem ao array embutido e mantém no máximo `CHAT_MAX_MESSAGES` (default 100) via `$slice: -N`. Documento embutido = sem tabela de mensagens separada. | `{"$push": {"messages": {"$each": [msg], "$slice": -100}}, "$set": {"updated_at": now}}` |
| `update_one` (auto-title) | `chat_memory.py:138-141` | Só define o título com a primeira mensagem do usuário, condicionado a `title == "Nova Conversa"` (idempotente). | `{"_id": oid, "title": "Nova Conversa"}` → `$set: {"title": "Quais índices…"}` |
| `find_one` (projeção) | `chat_memory.py:151-154` (`load_messages`) | Lê uma conversa inteira num único read (array embutido). Projeta só `messages` e `cluster`. | — |
| `aggregate` — pipeline de listagem | `chat_memory.py:167-179` (`list_conversations`) | `$match` por cluster (opcional) → `$project` com `$size: "$messages"` pra contar mensagens sem carregar o array inteiro → `$sort` por `updated_at` desc → `$limit`. Usa o índice composto `cluster_updated_at_idx`. | ```js [{"$match": {"cluster": "demo-cluster"}}, {"$project": {"cluster":1,"project_id":1,"title":1,"updated_at":1,"created_at":1,"msg_count":{"$size":"$messages"}}}, {"$sort": {"updated_at": -1}}, {"$limit": 25}] ``` |
| `find` + `$text` | `chat_memory.py:192-197` (`search_conversations`) | Busca textual usando o índice `text_search`, ordenada por relevância (`$meta: "textScore"`). | `{"$text": {"$search": "índice performance"}}`, `.sort([("score", {"$meta": "textScore"})])` |
| `delete_one` | `chat_memory.py:203` | Exclui uma conversa pelo `_id`. | — |

### 1.3 Guarda de injeção

`_oid()` (`chat_memory.py:47-53`) valida o `conversation_id` explicitamente
antes de instanciar `ObjectId` — string livre indo direto pro driver vira
`bson.errors.InvalidId` não tratado (500) em vez de erro de cliente (400).
Coberto por teste (`tests/`).

---

## 2. Ação direta no cluster observado — criação de índice via UI (Índices → Otimização)

### 2.1 `create_index_direct` — `atlas_client.py:608-676`

Único ponto de escrita **direta via pymongo** fora do assistente operacional.
Chamado por `POST /api/index` (`api.py:641-649`), que já validou namespace e
chaves antes de chegar aqui.

```python
name = mc[db_name][coll_name].create_index(keys)   # atlas_client.py:648
```

- `keys` vem da lista `index_keys` do corpo da requisição, convertida
  campo→direção (`1`/`-1`/`"hashed"`/`"2dsphere"`/`"text"`).
- Faz `collStats` antes de criar (`atlas_client.py:636-642`) para avisar se a
  coleção tem mais de 100 mil documentos — build pode demorar.
- Retry único em `OperationFailure` código `11602` (eleição de primário em
  andamento) com espera de 3s (`atlas_client.py:646-657`).
- Por que existe: fecha o loop do Performance Advisor — a tela mostra a
  sugestão, o operador aprova o namespace/chaves na UI, e o índice é criado no
  cluster real, sem precisar abrir um shell.

### 2.2 Guarda — validação de namespace e chaves (`api.py`, usada também por `assistant_tools.py`)

```python
_NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,63}\.[A-Za-z0-9_-]{1,120}$")
_PROTECTED_DATABASES = {"admin", "config", "local"}
_INDEX_FIELD_RE  = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_-]*)(?:\.[A-Za-z_][A-Za-z0-9_-]*)*$")
_INDEX_DIRECTIONS = {1, -1, "1", "-1", "hashed", "2dsphere", "text"}
```

Regras: namespace precisa ser `database.collection`; database não pode ser
`admin`/`config`/`local`; cada chave tem exatamente um campo; o nome do campo
casa a gramática de path (com subdocumento por ponto); direção/tipo vem de
lista fechada; no máximo 10 chaves por corpo. **Por que**: namespace vindo de
UI indo direto pro driver é injeção esperando acontecer — coberto por teste,
sem exigir cluster.

### 2.3 `/api/explain` — `api.py:612-638`

Roda `explain("executionStats")` real via `mc[db_name].command("explain",
{"find": coll, "filter": body.filter}, verbosity="executionStats")`
(`api.py:625-626`). Filtro passa por `_assert_filter_is_safe`
(`api.py:600-609`), que rejeita recursivamente `$where`, `$function`,
`$accumulator`, `$expr` — operadores que rodam JS/expressão arbitrária no
servidor. Devolve stage vencedor, `totalDocsExamined`, `totalKeysExamined`,
`nReturned`, tempo e índice usado (ou `"COLLSCAN (sem índice)"`). É o que
justifica visualmente o índice sugerido ao lado, na tela de Otimização — tempo
sozinho diz que está lento, o `explain` diz por quê.

---

## 3. Assistente operacional — 29 ferramentas MCP (`assistant_tools.py`)

Todas as ferramentas de dado são geradas dinamicamente por `ClusterTools`
(`assistant_tools.py:102-307`), vinculadas ao `project_id`/`cluster_name`
fixados pelo backend por rodada. Não existe MQL arbitrário nem shell — cada
ferramenta é um método fixo com schema JSON validado (`Draft202012Validator`).

### 3.1 Leitura (`ClusterTools.read`, `assistant_tools.py:152-216`)

| Ferramenta MCP | Query real | Linha | O que faz |
|---|---|---|---|
| `mongo_schema` | `coll.find({}).limit(25).max_time_ms(10000)` | `assistant_tools.py:189` | Descobre campos/tipos por amostragem de até 25 documentos (não é schema completo, não devolve valores). |
| `mongo_indexes` | `coll.list_indexes()` | `assistant_tools.py:200` | Lista índices existentes, paginado. |
| `mongo_search_indexes` | `coll.list_search_indexes()` | `assistant_tools.py:202` | Lista índices Atlas Search/Vector Search e seus estados. |
| `mongo_count` | `coll.count_documents(filt, maxTimeMS=10000)` | `assistant_tools.py:204` | Contagem exata sob filtro, com prazo. |
| `mongo_explain` | `mc[db].command("explain", {"find": collection, "filter": filt, "limit": 100, "maxTimeMS": 10000}, verbosity="executionStats")` | `assistant_tools.py:206` | Mesmo mecanismo do `/api/explain`, exposto ao modelo. |
| `mongo_find` | `coll.find(filt, projection).sort(...).skip(offset).limit(limit+1).max_time_ms(10000)` | `assistant_tools.py:209` | Consulta paginada com filtro/projeção/ordenação; busca `limit+1` para saber se há mais páginas sem contar tudo. |
| `mongo_aggregate` | `coll.aggregate(pipeline + [{"$skip": offset}, {"$limit": limit+1}], maxTimeMS=10000, allowDiskUse=False)` | `assistant_tools.py:210-212` | Agregação somente-leitura, inclusive `$search`/`$vectorSearch`; `allowDiskUse=False` de propósito — pipeline pesado deve ser rejeitado, não estourar disco em silêncio. |

Exemplo de pipeline aceito pelo modelo (roteiro de demo): "qual o total de
pedidos por status" vira algo como
`[{"$match": {...}}, {"$group": {"_id": "$status", "total": {"$sum": 1}}}]` —
gerado pelo Claude, validado pela gramática abaixo antes de rodar.

### 3.2 Guarda de pipeline — `validate_pipeline` (`assistant_tools.py:86-99`)

```python
READ_STAGES = {"$match","$project","$sort","$limit","$skip","$group","$count",
  "$unwind","$addFields","$set","$unset","$replaceRoot","$replaceWith",
  "$sortByCount","$bucket","$bucketAuto","$facet","$lookup","$sample",
  "$search","$searchMeta","$vectorSearch","$setWindowFields","$densify","$fill"}
```

Cada estágio precisa estar nessa lista fechada; `$out`/`$merge`/qualquer
estágio de escrita são recusados. `$lookup.from` é revalidado contra
`safe_namespace` (mesmo banco, coleções protegidas fora). `$facet` valida cada
sub-pipeline recursivamente. `safe_expression` (`assistant_tools.py:72-80`)
percorre filtro/update/validator/definição recursivamente proibindo
`$where`, `$function`, `$accumulator`, `$out`, `$merge`.

### 3.3 Escrita — só preparação (`ClusterTools.prepare`, `assistant_tools.py:218-251`) + execução isolada (`ClusterTools.execute`, `assistant_tools.py:253-307`)

Nenhuma ferramenta MCP grava direto. `prepare` faz o preflight (ex.: para
`mongo_update`/`mongo_delete`, roda `coll.find(filt, {"_id":1}).limit(101)…`
para congelar até 100 IDs e recusar lote maior — `assistant_tools.py:234-238`)
e devolve uma `proposal`; a execução real só roda depois de aprovação humana
no cartão, via `POST /api/assistant/actions/{id}`, que reivindica o token no
SQLite (`assistant_actions.py`) e então chama `execute`:

| Ferramenta (write) | Query de execução | Linha |
|---|---|---|
| `mongo_insert` | `coll.insert_many(documents, ordered=True)` | `assistant_tools.py:271` |
| `mongo_update` | `coll.update_many({"$and": [filter, {"_id": {"$in": frozen_ids}}]}, update)` | `assistant_tools.py:279` |
| `mongo_delete` | `coll.delete_many({"$and": [filter, {"_id": {"$in": frozen_ids}}]})` | `assistant_tools.py:281` |
| `mongo_create_index` | `coll.create_index(keys, unique=..., name=...)` | `assistant_tools.py:288` |
| `mongo_drop_index` | `coll.drop_index(name)` | `assistant_tools.py:290` |
| `mongo_create_collection` | `mc[db].create_collection(cn, validator=...)` | `assistant_tools.py:292` |
| `mongo_drop_collection` | `mc[db].drop_collection(cn)` | `assistant_tools.py:294` |
| `mongo_set_validator` | `mc[db].command("collMod", cn, validator=...)` | `assistant_tools.py:296` |
| `mongo_create/update/drop_search_index` | `coll.create_search_index(...)` / `update_search_index(...)` / `drop_search_index(...)` | `assistant_tools.py:298-305` |
| `atlas_scale` | `AtlasClient.scale_cluster(...)` (ver seção 4) | `assistant_tools.py:264` |

`filter` e `update`/`_id`s são **congelados na prévia** — documentos que
passem a corresponder entre prévia e aprovação não entram na execução, e o
filtro original é revalidado (registros alterados desde a prévia podem deixar
de corresponder). `mongo_update` só aceita operadores de atualização de uma
lista fechada (`$set`, `$unset`, `$inc`, `$mul`, `$min`, `$max`, `$push`,
`$pull`, `$addToSet`, `$pop`, `$rename` — `assistant_tools.py:126`);
substituição de documento inteiro/upsert não são permitidos.

---

## 4. Cluster observado — chamadas à Atlas Admin API v2 (`atlas_client.py`)

Não é MQL, mas é a fonte dos números da tela inteira. Todas passam pelo
retry de rate-limit `_with_rate_limit_retry` (`atlas_client.py:19-45`, honra
`Retry-After` em 429, até 3 tentativas).

| Método | Endpoint | Linha | Cache TTL |
|---|---|---|---|
| `get_org` | `GET /orgs/{org_id}` | `atlas_client.py:137-138` | — |
| `get_projects` | `GET /groups/{id}` ou `GET /groups` (paginado por `links.rel=next`) | `atlas_client.py:140-172` | 30s |
| `get_clusters` | `GET /groups/{project_id}/clusters` | `atlas_client.py:175-181` | 30s |
| `get_cluster` | `GET /groups/{project_id}/clusters/{cluster_name}` | `atlas_client.py:183-184` | — |
| `scale_cluster` | `PATCH /groups/{project_id}/clusters/{cluster_name}` (troca `instanceSize` em todos os `regionConfigs`) | `atlas_client.py:186-197` | — (**único ponto de escrita sobre a Admin API**) |
| `get_processes` | `GET /groups/{project_id}/processes` | `atlas_client.py:200-201` | — |
| `get_primary` | resolve via `connectionStrings`/hash do cluster, cai para heurística de hostname | `atlas_client.py:203-273` | 60s |
| `_disk_metrics` | `GET /processes/{id}/disks` + `GET /disks/{part}/measurements` (IOPS, latência, `%` de espaço, janela 5min) | `atlas_client.py:276-311` | — |
| `get_measurements` | `GET /processes/{id}/measurements` (CPU normalizada, memória, conexões, opcounters, rede, query targeting — janela 5min) | `atlas_client.py:314-403` | — |
| `get_measurements_series` | idem, série de 24h (`period=P1D`, `granularity=PT1H` por padrão) | `atlas_client.py:406-458` | — |
| `get_suggested_indexes` | `GET /processes/{id}/performanceAdvisor/suggestedIndexes` | `atlas_client.py:568-571` | — |
| `get_slow_queries` | `GET /processes/{id}/performanceAdvisor/slowQueryLogs?nLogs=N` | `atlas_client.py:561-568` | `/slow` pede `limit` linhas (padrão 1000, máx. 5000) e corta no servidor; o Health Score e o MCP seguem sem `nLogs` |
| `get_open_alerts` | `GET /groups/{project_id}/alerts?status=OPEN` | `atlas_client.py:579-586` | — |
| `get_pending_invoice` | `GET /orgs/{org_id}/invoices/pending` | `atlas_client.py:589-594` | — |

### Heurística de recomendação de escala — `recommend_scaling` (`atlas_client.py:470-565`)

Não é query MongoDB, mas é a lógica que consome os números acima e é
determinística/testada — está detalhada em `architecture.md`.

---

## 5. Seeds — carga real gerada no cluster de demo

Os dois scripts existem porque **Performance Advisor e Query Profiler ficam
vazios num cluster saudável e ocioso**; sem carga semeada antes, essas telas
aparecem em branco no meio da demo.

### 5.1 `populate_profiler.py` — 8 threads, ~25 minutos, banco `banco_inter`

Roda contra `transacoes` (`tx`) e `fatura` (`fat`), de propósito **sem
índice** nos campos consultados, para acionar sugestões do Performance
Advisor:

| Padrão | Exemplo real | Linha |
|---|---|---|
| Full scan por tipo | `tx.find({"amos_mt_type": typ}).limit(200)` | `populate_profiler.py:82` |
| Range em campo sem índice | `tx.find({"amos_mt_amount": {"$gt": amt_lo, "$lt": amt_hi}}).limit(100)` | `populate_profiler.py:86` |
| Filtro + sort sem índice composto (COLLSCAN + sort em memória) | `tx.find({"account_number": acc}).sort("amos_mt_amount", -1).limit(50)` | `populate_profiler.py:90` |
| Multi-campo sem composto | `tx.find({"segmento": seg, "amos_mt_type": typ, "amos_mt_plan": plan}).limit(50)` | `populate_profiler.py:106` |
| Range de data | `tx.find({"amos_mt_eff_date": {"$gte": d_tx[0], "$lte": d_tx[1]}}).limit(100)` | `populate_profiler.py:110` |
| Regex prefixo case-insensitive (lento de propósito) | `tx.find({"amos_mt_desc": {"$regex": f"^{prefix}", "$options": "i"}}).limit(30)` | `populate_profiler.py:117` |
| Agregação — total por categoria | `tx.aggregate([{"$match": {"segmento": seg, "amos_mt_type": typ}}, {"$group": {"_id": "$amos_mt_category_code", "total": {"$sum": {"$toDouble": "$amos_mt_amount"}}, "count": {"$sum": 1}}}, {"$sort": {"total": -1}}, {"$limit": 10}])` | `populate_profiler.py:121-128` |
| Agregação — perfil por conta (`$avg`) | `tx.aggregate([{"$match": {"account_number": acc}}, {"$group": {"_id": "$amos_mt_category_code", "total": {...}, "qtd": {"$sum": 1}, "media": {"$avg": {...}}}}, {"$sort": {"total": -1}}, {"$limit": 5}])` | `populate_profiler.py:140-148` |
| Agregação — top gastadores | `tx.aggregate([{"$match": {"segmento": seg}}, {"$group": {"_id": "$account_number", "total": {...}}}, {"$sort": {"total": -1}}, {"$limit": 10}])` | `populate_profiler.py:151-157` |
| Agregação — volume diário | `tx.aggregate([{"$match": {"segmento": seg}}, {"$group": {"_id": "$diaria_data", "volume": {...}, "count": {"$sum": 1}}}, {"$sort": {"_id": -1}}, {"$limit": 30}])` | `populate_profiler.py:160-167` |
| Count sem índice | `tx.count_documents({"segmento": seg, "amos_mt_type": typ})` | `populate_profiler.py:170` |

Todas as variantes se repetem em espelho para `fat` (`fatura`).

### 5.2 `populate_workload.py` — threads de leitura ponderadas + thread de escrita

Mesmo espírito, com pesos por forma de query (`shapes`,
`populate_workload.py:97-146`) e `max_time_ms`/`maxTimeMS` explícitos em cada
chamada. Além disso tem uma thread de escrita dedicada
(`write_worker`, `populate_workload.py:163-187`) que gera pressão de
escrita real:

```python
ev.insert_many(docs)                       # populate_workload.py:176 — 200 docs/lote em eventos_pix
ev.update_many(                            # populate_workload.py:178-181
    {"status": "pendente", "valor": {"$lt": 1000}},
    {"$set": {"status": "liquidado"}},
)
```

O `update_many` roda **sem índice em `status`** de propósito — gera scan +
carga de escrita simultânea, pra dar ao Performance Advisor um caso de índice
em campo de escrita frequente, não só leitura.

---

## Resumo quantitativo

- **Índices explícitos criados pela aplicação**: 5, todos em
  `torre.chat_history` (`chat_memory.py`) — 1 text, 3 single/compound de
  suporte a listagem/filtro, 1 TTL.
- **Ferramentas MCP de leitura de dado**: 10 (`mongo_*` leitura + `atlas_*`
  leitura, ver seção 3.1 e o catálogo completo em `assistant_tools.py:24-51`).
- **Ferramentas MCP de escrita (preparação + execução aprovada)**: 15
  (`mongo_insert/update/delete/create_index/drop_index/create_collection/
  drop_collection/set_validator/create_search_index/update_search_index/
  drop_search_index` + `atlas_scale`).
- **Endpoints REST que tocam o cluster observado diretamente**: `/api/explain`
  e `/api/index` (`api.py`), além de `create_index_direct`
  (`atlas_client.py:608`).
- **Chamadas de leitura à Atlas Admin API v2**: 13 métodos distintos em
  `atlas_client.py`, com cache TTL em 3 delas (projetos, clusters, primário).
- **Único ponto de escrita sobre a Admin API**: `scale_cluster`
  (`atlas_client.py:186`).

## Insights e carga somente leitura — v3.1.0

`atlas_cluster_insights` usa os processos identificados exatamente pelo cluster e alinha CPU dos nós em buckets UTC de cinco minutos. O histórico mantém pontos nulos; cobertura simultânea é requisito para recomendar redução.

`mongo_explain` aceita `sort` validado (direções 1 ou -1) e limite de até 100 documentos. Consultas lentas são ordenadas pelas mais recentes e paginadas.

`stress_readonly.py` usa um único pool de conexão, divide leituras entre primário e secundários, limita concorrência e duração e aplica `maxTimeMS`. O workload é identificado por `appName`/`comment`, permitindo distinguir carga sintética de tráfego orgânico nos relatórios. Não grava no dataset.

## Exceção documentada: `$regex` nos geradores de carga

Nenhuma query da aplicação usa `$regex` (busca textual é `$search`). As ferramentas de dados do assistente recusam `$regex` e `$regularExpression` em `mongo_find`, `mongo_count` e `mongo_aggregate`; só `mongo_explain` e `POST /api/explain` aceitam, porque é assim que a slow query do Profiler é diagnosticada (`explain` com `maxTimeMS: 10000`). A exceção são os três geradores de carga, que usam regex de prefixo case-insensitive de propósito para produzir a query lenta (COLLSCAN) que o Profiler e o Performance Advisor precisam mostrar: `populate_profiler.py`, `populate_workload.py` e `stress_readonly.py`. Cada ocorrência tem o comentário `EXCEÇÃO EXPLÍCITA`, e `tests/test_hardening_adversarial.py` falha se `$regex` aparecer em qualquer outro módulo. `populate_workload.py` só escreve com `ALLOW_DEMO_DB_WRITE=1` e marca os documentos com `_torre_workload: true`, que `scripts/reset_demo.py` remove.

