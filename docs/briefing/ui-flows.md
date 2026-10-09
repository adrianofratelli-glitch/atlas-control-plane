# Torre — Atlas Control Plane — Interface, telas e fluxos

> Quem olha essa tela é DBA, SRE ou quem paga a fatura do Atlas. Arquitetura
> geral em `architecture.md`, queries/índices em `queries.md`, comportamento
> do agente em `agent-behavior.md`.

## Navegação (`frontend/src/App.jsx`)

Sete destinos no menu lateral (`NAV`, `App.jsx:17-25`), cada um lazy-loaded a
partir de `frontend/src/pages/`:

| id | Label na UI | Componente | Ícone | Screenshot |
|---|---|---|---|---|
| `overview` | Visão Geral | `Overview.jsx` | Dashboard | `docs/screenshots/01-overview.png` |
| `indexes` | Índices | `PerformanceAdvisor.jsx` | LightningBolt | — |
| `profiler` | Consultas | `Profiler.jsx` | MagnifyingGlass | — |
| `health` | Saúde | `Health.jsx` | Gauge | `docs/screenshots/02-health-score.png` |
| `scale` | Escala | `Scale.jsx` | Charts | `docs/screenshots/03-scale.png` |
| `finops` | FinOps | `FinOps.jsx` | Coin | `docs/screenshots/04-finops.png` |
| `chat` | Assistente | `Chat.jsx` | Sparkle | `docs/screenshots/05-ai-chat.png` |

`Clusters.jsx` e `Compare.jsx` foram removidos na 3.2.0: estavam fora do menu, e o
que mostravam (lista de clusters, health lado a lado) já está em Visão Geral e Saúde.
A navegação fica em sete destinos operacionais.
Índices/Consultas continuam acessíveis como módulos auxiliares que alimentam o
resto (Performance Advisor e Query Profiler ficam vazios em cluster ocioso,
por isso os `populate_*` scripts existem — ver `queries.md`, seção 5).

A aba `chat` é tratada como caso especial: fica sempre montada
(`<div hidden={active !== 'chat'}>`, `App.jsx:76-80`) para não perder a
conversa em andamento ao trocar de página — as outras trocam via `Suspense` a
cada clique.

## Contrato com o backend (`frontend/src/api.js`)

Uma função por endpoint, todas sobre `axios` com `baseURL: '/api'` e
`timeout: 60000` (a Admin API é lenta; valor medido, não chute —
`api.js:10`). Token opcional via `VITE_API_TOKEN` → header `Authorization:
Bearer`.

| Grupo | Funções | Endpoint |
|---|---|---|
| Frota | `getConfig`, `getClusters`, `getAlerts`, `getInvoice` | `/config`, `/clusters`, `/alerts`, `/invoice` |
| Por cluster | `getPA`, `getSlow`, `getMeasurements`, `getSeries`, `getHealth` | `/cluster/{project_id}/{cluster_name}/{pa,slow,measurements,series,health}` |
| Escala | `getLiveMetrics`, `getScaling`, `scaleCluster` | `GET .../live`, `GET .../scaling`, `POST .../scale` |
| Otimização | `explainQuery`, `createIndex` | `POST /explain`, `POST /index` |
| FinOps | `getFinops`, `streamAssistant` | `/finops`, `/assistant` (`mode=report`) |
| Histórico de chat | `listConversations`, `getConversation`, `deleteConversation` | `/chat/conversations...` |
| Relatório | `downloadReport` (fetch, resposta binária PDF) | `POST /report` |

`project_id` + `cluster_name` é a chave em quase toda rota; `_picker.jsx`
(`frontend/src/pages/_picker.jsx`) é o seletor de cluster compartilhado entre
Índices, Consultas, Saúde, Escala e Assistente — evita comparar métrica de um
cluster com custo de outro sem perceber.

### Streaming

`axios` cobre os GETs, mas `/chat`, `/analyze` e `/api/assistant` (NDJSON)
usam `fetch` cru com `getReader()` porque `axios` não expõe `ReadableStream`
no browser (`api.js:31-53`, função `streamPost`). O texto é acumulado e
entregue por callback para renderizar markdown parcial enquanto o token chega.
`downloadReport` também usa `fetch` puro, porque a resposta é PDF binário, não
JSON.

## As telas

### Overview — a frota inteira numa tela

`docs/screenshots/01-overview.png`. Primeiro slide de qualquer conversa:
quantos projetos, quantos clusters, o que está alertando agora — sem scroll.
Consome `/config`, `/clusters`, `/alerts`, `/invoice`.

### Índices (Performance Advisor)

Lista as sugestões reais do Performance Advisor (`/cluster/.../pa`) e permite
criar o índice sugerido direto no cluster (`POST /index`, ver `queries.md`
seção 2). Fica fora do menu principal desde a reorganização "modo palco", mas
continua acessível — é a fonte usada pelo roteiro de demo do assistente.

### Consultas (Query Profiler)

Slow queries reais (`/cluster/.../slow`, últimas 1000 linhas do slow log por padrão,
`?limit=` até 5000; a tela diz quantas entradas agrupou) com `explain` ao lado
(`POST /explain`) mostrando `totalKeysExamined` e o plano — tempo sozinho diz
que está lento, o `explain` diz por quê.

### Health — `docs/screenshots/02-health-score.png`

Alertas abertos e o score, com os problemas que o compõem, consolidados numa
tela só (sem abrir páginas de diagnóstico separadas). Consome
`/cluster/.../health`.

### Scale — `docs/screenshots/03-scale.png`

A tela mais sensível da PoV. Mostra a recomendação (`GET /scaling`; a UI manda `tier`, e sem ele a API usa o tier atual do cluster) e o botão
de execução (`POST /scale`, único ponto de escrita direta do frontend sobre a
Admin API — `scaleCluster` em `api.js:21`). **Regra de diagramação: o número
da heurística e a justificativa do Claude ficam separados literalmente no
layout**, nunca misturados num parágrafo — é o que sustenta a resposta pronta
para "esse tier foi o modelo que chutou?" (ver `architecture.md`). A tela
também precisa indicar qual janela temporal sustentou a decisão ("CPU (p95
24h)" vs. só "CPU"), porque a mesma recomendação com base diferente é uma
recomendação diferente.

### FinOps — `docs/screenshots/04-finops.png`

Custo por cluster, média de CPU dos nós, pior p95 e cobertura. Consome `/finops`; fatura indisponível e clusters pausados têm estados explícitos. O botão de análise usa MCP real e mostra progresso, erro e relatório exportável. A análise permanece montada ao trocar de aba.

Escala coleta `/live` a cada cinco segundos, mostra média e nó mais carregado, RAM/disco/conexões do primário e tabela individual com timestamps. Sem gráficos 24h ou aba de demonstração do protocolo MCP. Os valores só mudam quando a fonte fornece nova amostra.

### Assistente (Chat) — `docs/screenshots/05-ai-chat.png`

Chat com histórico persistido (`/chat/conversations`), resposta em streaming
sobre dado real da Admin API/MongoDB via o loop de ferramentas MCP (ver
`agent-behavior.md`), com o badge "Ver trace no Langfuse" aparecendo no
cabeçalho assim que o evento `trace_url` chega no stream — antes mesmo da
resposta terminar (`docs/assistant.md`, seção Langfuse).

Screenshot deliberadamente mostra o assistente recusando estimar métrica de
24h quando só existe janela de 5 minutos — recusar-se a inventar é o ponto da
demo, não um defeito a esconder.

## Stack de UI e decisões que custaram tempo (`frontend/vite.config.js`)

| Item | Escolha | Motivo |
|---|---|---|
| Build | Vite, portas por env (`WEB_PORT` 5290, `API_PORT` 8765) | a máquina roda várias PoVs; portas configuráveis sem editar código |
| UI kit | LeafyGreen | painel de operação Atlas — design system do próprio MongoDB |
| Estado | `useState` no `App.jsx` | cinco páginas primárias, cada uma busca o próprio dado; sem estado global real |
| Navegação | estado `active` + menu compacto, sem router | não vale a dependência para sete destinos |
| Markdown | `react-markdown` + `remark-gfm` | respostas do Claude vêm em markdown com tabela |
| `nodePolyfills` | Buffer/global/process | dependências transitivas do LeafyGreen (`readable-stream`, `through`) usam globais do Node ausentes no browser |
| `manualChunks` | separa `leafygreen`, `markdown`, `vendor` | evita bundle único > 500 kB e melhora cache do browser entre deploys |

`frontend/src/styles.css` traz os tokens dark do MongoDB
(`--bg-primary`, `--accent`, `--text-pri/sec/muted`) e tipografia Special Gothic +
Source Code Pro — mesma paleta das outras PoVs do portfólio.

### Assinatura visual do portfólio (v2)

`src/pov-signature.css` é cópia sincronizada entre os onze frontends do
workspace, importada **depois** do stylesheet local. O contêiner raiz carrega
`data-pov-shell`; há um `.pov-skip-link` para `#conteudo-principal`; o
`index.html` declara pt-BR, dark color scheme e favicon comum. Qualquer
mudança na assinatura precisa ser replicada nas onze cópias e validada em
1440/768/360 px, além do build de produção e do estado offline.

## Duas regras que valem para toda tela

1. **O frontend nunca vê credencial.** O browser só fala com `/api`.
2. **A tela não calcula recomendação.** O React exibe resultado e
   justificativa — não reimplementa a heurística de escala nem qualquer outra
   regra determinística do backend.

## Antes de apresentar

- Rodar `populate_workload.py` e `populate_profiler.py` — senão Índices e
  Consultas ficam vazios.
- Uma pergunta de aquecimento no chat, para pagar o cold start fora da demo.
- Se for capturar tela: nomes reais de projeto/cluster identificam um
  cliente (aparecem no header, no `<select>` de cluster de todas as páginas,
  na tabela do FinOps e na pílula de contexto do chat) — substituir no DOM
  (nós de texto **e** rótulos de `<option>`) imediatamente antes da captura,
  nunca recortar depois.
