# ADR-001 — Identidade do cluster pelo host, não pelo hash do projeto

- Status: aceito (2026-11-12)
- Contexto do código: `backend/cluster_identity.py`, `backend/api.py` (`_assert_uri_targets`, `_uri_targets`)

## Contexto

As ações diretas no MongoDB (explain, criar índice pela UI, ferramentas `mongo_*` do assistente e a
execução das aprovações) rodam no cluster de `MONGODB_URI`. Antes de executar, a Torre confere se esse
cluster é o mesmo selecionado na tela; se não for, recusa com 409.

A conferência comparava só o segundo rótulo do host SRV (`<cluster>.<hash>.mongodb.net`). Esse `<hash>`
é **do projeto**: todos os clusters do projeto o compartilham. No teste ao vivo de 2026-11-12, os três
clusters do projeto de demo vieram com `is_uri_target: true`, e `POST /api/explain` com um cluster
**pausado** selecionado devolveu um plano real, executado no cluster do `MONGODB_URI`. A suíte offline
não pegou porque cada fixture usava um hash diferente por cluster.

## Decisão

A identidade passa a ser a **interseção de hosts**: os hosts de `MONGODB_URI` (SRV ou lista de membros,
sem credencial, porta ou caixa) precisam aparecer entre os hosts que a Admin API publica para aquele
cluster em `connectionStrings` (`standardSrv`, `standard`, `private`, `privateSrv`, `privateEndpoint`).
Funções puras em `backend/cluster_identity.py`; a API só as chama.

- Cluster sem host publicado → 503 (fail closed, como antes).
- Admin API indisponível → 503.
- Host diferente → 409.

## Alternativas descartadas

- **Comparar o nome do cluster com o primeiro rótulo do host**: o Atlas trunca nomes longos e acrescenta
  sufixo; URIs com lista de membros usam `<nome>-shard-00-0N`. Frágil.
- **Perguntar ao próprio `mongod` (`hello.setName`) e cruzar com `processes` da Admin API**: correto, mas
  abre conexão e soma chamadas a cada ação; a interseção de hosts usa o `get_cluster` que já era feito.

## Consequências

- `is_uri_target` marca um único cluster; botões de criar índice/explain somem dos demais.
- O assistente, com outro cluster selecionado, recebe 409 nas ferramentas de dados em vez de ler o
  cluster do `MONGODB_URI`. Medido no eval ao vivo (`eval/run_assistant_live.py`, caso
  `outro_cluster_nao_le_dados_do_uri`): falhava antes, passa depois.
- Regressão coberta por `tests/test_cluster_identity.py` (clusters irmãos com o mesmo hash de projeto).
