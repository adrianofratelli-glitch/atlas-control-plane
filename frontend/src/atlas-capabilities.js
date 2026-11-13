// What each screen demonstrates in Atlas, and the calls the SERVER makes for it.
// Paths are the Atlas Admin API v2 paths used by backend/atlas_client.py (base
// https://cloud.mongodb.com/api/atlas/v2); tests/test_atlas_capabilities.py fails if a
// path here is not called there, so this panel cannot drift from the code.
// Driver commands go through MONGODB_URI (backend/api.py) and only run on that cluster.

export const ATLAS_CAPABILITIES = {
  overview: {
    capability: 'Atlas Admin API v2 · inventário da organização',
    why: 'Frota, estado, versão e alertas vêm da API de gestão do Atlas; custo é estimativa de tabela (sem chamada).',
    calls: [
      'GET /groups',
      'GET /groups/{groupId}/clusters',
      'GET /groups/{groupId}/alerts?status=OPEN',
    ],
  },
  indexes: {
    capability: 'Performance Advisor (Admin API) · criação de índice pelo driver',
    why: 'O Atlas sugere índices a partir das consultas lentas do primário; o botão executa createIndexes no cluster do MONGODB_URI.',
    calls: [
      'GET /groups/{groupId}/processes',
      'GET /groups/{groupId}/processes/{processId}/performanceAdvisor/suggestedIndexes',
    ],
    driver: ['createIndexes (db.<coleção>.createIndex) — só no cluster do MONGODB_URI'],
  },
  profiler: {
    capability: 'Slow query log do Performance Advisor · explain(executionStats) pelo driver',
    why: 'As consultas vêm do log de consultas lentas do Atlas; o explain roda de verdade no cluster do MONGODB_URI e mostra o comando enviado.',
    calls: [
      'GET /groups/{groupId}/processes',
      'GET /groups/{groupId}/processes/{processId}/performanceAdvisor/slowQueryLogs?nLogs=1000',
    ],
    driver: ['explain { find, filter, sort, limit } verbosity executionStats — só no cluster do MONGODB_URI'],
  },
  health: {
    capability: 'Admin API v2 · estado do cluster + Performance Advisor',
    why: 'A nota combina estado e versão lidos do Atlas, índices sugeridos e COLLSCANs do slow log. Sem leitura confiável, não há nota.',
    calls: [
      'GET /groups/{groupId}/clusters/{clusterName}',
      'GET /groups/{groupId}/processes',
      'GET /groups/{groupId}/processes/{processId}/performanceAdvisor/suggestedIndexes',
      'GET /groups/{groupId}/processes/{processId}/performanceAdvisor/slowQueryLogs',
    ],
  },
  scale: {
    capability: 'Métricas de processo (Admin API) · mudança de tier com aprovação',
    why: 'CPU, memória e disco por nó vêm das métricas do Atlas; a recomendação é uma heurística determinística, não o modelo. O PATCH só sai depois da sua confirmação.',
    calls: [
      'GET /groups/{groupId}/clusters/{clusterName}',
      'GET /groups/{groupId}/processes',
      'GET /groups/{groupId}/processes/{processId}/measurements',
      'GET /groups/{groupId}/processes/{processId}/disks',
      'PATCH /groups/{groupId}/clusters/{clusterName}  (só após confirmar)',
    ],
  },
  finops: {
    capability: 'Billing API + métricas (Admin API) · FinOps',
    why: 'A fatura corrente é real (Billing API); custo por cluster é estimativa de tabela cruzada com a CPU de 24 h de cada nó.',
    calls: [
      'GET /orgs/{orgId}/invoices/pending',
      'GET /groups/{groupId}/clusters',
      'GET /groups/{groupId}/clusters/{clusterName}',
      'GET /groups/{groupId}/processes',
      'GET /groups/{groupId}/processes/{processId}/measurements',
    ],
  },
  chat: {
    capability: 'Assistente via MCP · ferramentas sobre a Admin API e o driver',
    why: 'O modelo (pelo gateway) chama ferramentas MCP; cada uma é uma das chamadas acima ou uma leitura no cluster do MONGODB_URI. Escrita só prepara um cartão para aprovação.',
    calls: [
      'Ferramentas atlas_* → Admin API v2 (mesmas rotas das telas)',
      'Ferramentas mongo_* → driver, só no cluster do MONGODB_URI',
    ],
  },
}
