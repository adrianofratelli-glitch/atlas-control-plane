# Assistente operacional da Torre

O cliente descreve o que precisa em português. O Assistente consulta ferramentas MCP, interpreta os resultados e prepara alterações para aprovação na própria conversa. Não é necessário escrever MQL.

## Conexão e execução

React → `/api/assistant` (NDJSON) → modelo configurado → SDK MCP via stdio → `torre_mcp_server.py` → Atlas Admin API / PyMongo.

O servidor MCP é iniciado automaticamente por rodada, com projeto e cluster fixados pelo backend. As ferramentas de alteração fazem apenas a prévia. A execução acontece em `/api/assistant/actions/{id}`, após o clique em **Aprovar e executar**, usando os argumentos gravados no servidor. O modelo não recebe uma ferramenta para aprovar ou executar ações.

O seletor de cluster é compartilhado com Índices, Consultas, Saúde e Escala. A conversa permanece ao navegar; mudar de cluster inicia outro contexto. Históricos novos registram projeto e cluster. Históricos antigos sem identificação de projeto não são usados para retomar operações, evitando associar clusters homônimos incorretamente.

## Operações disponíveis

| Área | Consultas | Alterações com aprovação |
|---|---|---|
| Atlas | Configuração, métricas de 5 min, séries de 24 h, alertas do projeto, custo estimado | Tier do cluster |
| Performance | Todas as páginas do Performance Advisor, slow queries, índices existentes, explain | Criar/remover índice |
| Dados | Bancos, coleções, amostra de campos/tipos, find, count, aggregate | Inserir, atualizar e excluir documentos |
| Coleções | Descobrir estrutura observada | Criar/excluir coleção, definir validator |
| Search / Vector | Listar índices e estados; agregações com `$search`/`$vectorSearch` | Criar, atualizar e remover índices |

São 28 ferramentas. Não há shell, MQL arbitrário com efeitos colaterais, administração de usuários/IAM, exclusão de banco ou backup/restore neste catálogo. Novas operações devem ser registradas em `assistant_tools.py`, com schema, prévia e testes. Não anuncie suporte irrestrito a toda a API MongoDB.

## Configuração

Instale `requirements.txt`; `run_react.sh` também verifica a presença do SDK MCP. São usadas as mesmas variáveis Atlas, `MONGODB_URI`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL` e `CLAUDE_MODEL` do restante da aplicação. O gateway personalizado conserva o cabeçalho `api-key` existente; sem gateway, usa autenticação nativa Anthropic. Credenciais não são enviadas ao frontend.

A conexão `MONGODB_URI` é validada contra o cluster selecionado antes de cada operação de dados e novamente antes da execução. Se não corresponder, a operação é recusada. Uma única URI não dá acesso aos dados de todos os clusters da organização; a API Atlas pode listar outros clusters, mas a conexão de dados precisa corresponder ao alvo escolhido.

`API_AUTH_TOKEN` protege também os novos endpoints. O projeto mantém o modelo de autenticação compartilhada da demonstração, não implementa RBAC individual ou isolamento de usuários de uma plataforma SaaS. Restrinja o servidor ao ambiente de demonstração autorizado.

O histórico de aprovações fica em `.assistant-state/actions.sqlite3` (ignorado pelo Git, acesso local restrito). `TORRE_ACTION_DB` permite escolher outro caminho. Em Docker, monte armazenamento persistente nesse diretório para conservar aprovações entre recriações do container. Vários workers no mesmo host compartilham o SQLite; múltiplas réplicas distribuídas precisam de armazenamento transacional compartilhado antes de publicação nesse formato.

## Limites e estados

- Resultados paginados: até 100 documentos por página. O modelo recebe JSON válido; páginas grandes são reduzidas com `next_offset`, sem cortar o JSON no meio.
- Atualização/exclusão: filtro explícito, até 100 documentos, IDs congelados na prévia. Novos documentos que passem a corresponder não entram na execução. O filtro original é revalidado; registros alterados desde a prévia podem deixar de corresponder. Não há transação em lote.
- Inserção: até 100 documentos por pedido. Falhas podem deixar inserções parciais; não há repetição automática.
- Coleção inteira: exclusão explícita, com estimativa de documentos e aprovação. O conteúdo pode mudar entre prévia e execução.
- Consultas: prazo de 15 s no driver, comandos com `maxTimeMS` quando aplicável; escritas: 30 s no driver. O Atlas tem seus próprios prazos HTTP.
- Aprovação: validade de 15 minutos, vinculada à sessão do navegador, projeto e cluster. Tokens são de uso único, com reivindicação atômica no SQLite.
- `completed` significa resultado recebido. Tier e Search podem retornar `submitted`: a operação assíncrona ainda deve ser consultada até concluir.
- `unknown`: falha após o início; pode haver alteração parcial. Consulte o estado antes de preparar outra operação. A mesma aprovação nunca é reenviada.
- Cancelar a conversa interrompe a análise; não desfaz nem confirma operações. **Atualizar ações** recupera os estados gravados no servidor.
- Histórico de ações: últimos 20 itens por sessão/contexto, retenção de sete dias; execuções sem resultado são preservadas. Limite de 20 aprovações pendentes por sessão.
- Rodada: até 12 etapas, 40 ferramentas e 240 s. Respostas parciais ou interrompidas são sinalizadas.

## Roteiro de demonstração

1. Em **Índices**, selecione o cluster. Abra **Assistente**: a seleção será a mesma.
2. “Liste todos os índices recomendados e explique quais consultas eles melhoram.” Veja as consultas MCP e a distinção entre ausência de sugestões e erro de acesso.
3. “Mostre os bancos e coleções; explique os campos da coleção de pedidos.” O Assistente descobre a estrutura observada.
4. “Qual o total de pedidos por status?” O Assistente gera a agregação sem exigir MQL.
5. Em uma coleção de demonstração: “Prepare a atualização dos pedidos com status teste para aprovado.” Confira alvo, campos e quantidade; aprove no cartão.
6. “Confira se a atualização foi aplicada.” O Assistente volta a consultar os dados.

Somente o passo 5 altera dados: escolha um conjunto de demonstração autorizado. Os testes automatizados usam mocks para gravações; o teste de integração MCP usa uma consulta de estimativa sem acesso ao banco.

## Verificação

```bash
venv/bin/python -m unittest discover -s tests -v
cd frontend
node --test tests/*.test.mjs
npm run build
```

Testes cobrem paginação, erros do Advisor, bloqueio de `$out`/`$merge`/JavaScript, isolamento do cluster, IDs congelados, aprovação concorrente, expiração/cancelamento/reinício, resultado incerto, protocolo MCP real e ciclo de ferramentas sem execução antecipada.

Referências: [SDK MCP Python 1.x](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x), [tool use Anthropic](https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools).

O teste visual `tests/browser-assistant.cjs` usa Playwright e intercepta todas as APIs com fixtures. Com a interface local iniciada, execute `node tests/browser-assistant.cjs`; `PLAYWRIGHT_MODULE` pode apontar para uma instalação existente do Playwright. Verifica seleção entre abas, aprovação única, preservação da conversa e viewport mobile. Nenhuma escrita chega ao Atlas nesse teste.

## Observability opcional: Langfuse

Com `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` no `.env` (`tracing.py`), cada turno vira uma trace: uma generation por passo de raciocínio do LLM, um span por tool call MCP. Fail-open — sem as chaves, ou com o Langfuse fora do ar, vira no-op e o turno segue normal (um `auth_check()` roda uma vez por processo pra nunca expor um link que dê 404 no meio de uma demo). O evento `trace_url` sai no stream logo após `connected`, e o Chat renderiza o badge "Ver trace no Langfuse" no cabeçalho assim que chega — antes mesmo da resposta terminar.
