import { useState, useEffect, useRef } from 'react'
import { H1, Body } from '@leafygreen-ui/typography'
import Banner from '@leafygreen-ui/banner'
import Badge from '@leafygreen-ui/badge'
import Button from '@leafygreen-ui/button'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { KpiGrid, Kpi, Section } from '../components.jsx'
import { getFinops, getInvoice, streamAssistant, downloadReport } from '../api.js'

const fmt = (n) => Math.round(n).toLocaleString('pt-BR')
const pct = (n) => n == null ? '—' : `${n}%`
const VAR = { green: 'green', yellow: 'yellow', red: 'red', muted: 'lightgray' }

export default function FinOps({ clusters }) {
  const [data, setData] = useState(null)
  const [invoice, setInvoice] = useState(null)
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [analysis, setAnalysis] = useState('')
  const [analysisCluster, setAnalysisCluster] = useState('')
  const [busyAI, setBusyAI] = useState(false)
  const [tools, setTools] = useState([])
  const [phase, setPhase] = useState('')
  const [elapsed, setElapsed] = useState(0)
  const analysisLock = useRef(false)
  const request = useRef(null)
  const load = async () => {
    setBusy(true); setError('')
    try { setData(await getFinops()) }
    catch (e) { setData(null); setError(e?.response?.data?.detail || e.message) }
    finally { setBusy(false) }
  }
  useEffect(() => {
    load()
    getInvoice().then(setInvoice).catch(() => setInvoice(null))
    return () => request.current?.abort()
  }, [])

  const analyze = async (c) => {
    if (analysisLock.current) return
    analysisLock.current = true
    setElapsed(0); setPhase('Conectando à sessão MCP')
    const started = Date.now()
    const ticker = setInterval(() => setElapsed(Math.floor((Date.now() - started) / 1000)), 1000)
    setBusyAI(true); setAnalysis(''); setAnalysisCluster(c.cluster); setTools([]); setError('')
    const controller = new AbortController()
    request.current = controller
    const timer = setTimeout(() => controller.abort(), 190000)
    try {
      for await (const event of streamAssistant({
        mode: 'report', session_id: crypto.randomUUID(), project_id: c.project_id, cluster_name: c.cluster,
        messages: [{ role: 'user', content: 'Gere um relatório técnico de FinOps e performance, somente leitura. Consulte atlas_cluster_insights, atlas_cluster, atlas_indexes e atlas_slow_queries. Correlacione média dos nós, p95 por nó, janela recente, cobertura, RAM, disco, I/O e conexões. Use o plano registrado nas slow queries como evidência. Explain aprofundado pode ser feito depois no Assistente; não invente validação de ganho. Indique gargalos por nó, alternativas de otimização antes de escala, tier candidato e economia estimada via atlas_cost. Não prepare ações. Distinga fatos, hipóteses e dados ausentes; não recomende redução com cobertura incompleta. Máximo 450 palavras. Use seções curtas, sem tabelas extensas. Priorize as slow queries mais recentes e identifique eventual carga sintética pelo comentário. Não atribua planningTimeMicros à geração de embeddings sem timings que provem essa causa.' }],
      }, controller.signal)) {
        if (event.type === 'text') setAnalysis(a => a + event.text)
        if (event.type === 'connected') setPhase('Sessão MCP conectada · coletando evidências')
        if (event.type === 'tool_start') { setPhase(event.label || 'Consultando MCP'); setTools(t => [...t, { ...event, running: true }]) }
        if (event.type === 'tool_end') setTools(t => t.map(item => item.id === event.id ? { ...event, running: false } : item))
        if (event.type === 'model_start') setPhase('Claude correlacionando evidências · resposta em preparação')
        if (event.type === 'text') setPhase('Recebendo resposta do Claude')
        if (event.type === 'error') setError(event.message)
      }
    } catch (e) { setError(e.name === 'AbortError' ? 'Análise interrompida.' : e.message) }
    finally { clearTimeout(timer); clearInterval(ticker); analysisLock.current = false; request.current = null; setBusyAI(false) }
  }

  const rows = data?.clusters || []
  const active = rows.filter(c => c.status !== 'paused')
  const incomplete = active.filter(c => !c.complete)
  const down = active.filter(c => c.action === 'down')
  const up = active.filter(c => c.action === 'up')
  const totalBrl = clusters.reduce((s, c) => s + c.cost_brl, 0)
  const totalUsd = clusters.reduce((s, c) => s + c.cost_usd, 0)
  let verdict = { variant: 'info', text: 'Nenhum cluster ativo avaliado.' }
  if (active.length) verdict = { variant: 'info', text: 'CPU avaliada. Valide working set, I/O e sazonalidade antes de mudar o tier.' }
  if (incomplete.length) verdict = { variant: 'warning', text: `${incomplete.length} cluster(s) com histórico incompleto — redução de tier bloqueada. Veja cobertura e evidências por nó.` }
  if (down.length) verdict = { variant: 'info', text: `${down.length} candidato(s) a redução — economia condicionada à validação do tier alvo.${incomplete.length ? ' Há clusters com histórico incompleto.' : ''}` }
  if (up.length) verdict = { variant: 'warning', text: `${up.length} cluster(s) com pressão de CPU em pelo menos um nó. Investigue queries e distribuição antes de decidir escala.${incomplete.length ? ' Há histórico incompleto.' : ''}` }

  return <>
    <div className="page-head"><H1 style={{ color: 'var(--text-pri)' }}>FinOps</H1><Button onClick={load} disabled={busy || busyAI}>Atualizar métricas</Button></div>
    <KpiGrid>
      <Kpi label="Fatura Corrente" value={invoice == null ? '—' : `$${fmt(invoice)}`} delta={invoice == null ? 'indisponível · Atlas Billing API' : 'real · Atlas Billing API'} />
      <Kpi label="Total USD/Mês (est.)" value={`$${fmt(totalUsd)}`} delta="referência us-east-1 · inclui pausados" />
      <Kpi label="Total BRL/Mês (est.)" value={`R$ ${fmt(totalBrl)}`} />
      <Kpi label="Média/Cluster (est.)" value={`R$ ${fmt(clusters.length ? totalBrl / clusters.length : 0)}`} />
      <Kpi label="Economia Candidata" value={busy ? '…' : !data || (incomplete.length && !down.length) ? '—' : `$${fmt(data.potential_savings_usd)}`}
           delta={busy ? 'avaliando…' : down.length ? 'condicionada · validar tier alvo' : 'sem economia confirmada'} />
    </KpiGrid>
    {error && <Banner variant="danger">{error}</Banner>}
    {!busy && data && <Banner variant={verdict.variant} style={{ marginBottom: 18 }}>{verdict.text}</Banner>}
    <Section title="Eficiência por Cluster" sub="CPU média dos nós em intervalos comuns de 5min · p95 do nó mais carregado · janela de 24h" />
    {busy && <Body>Avaliando os nós…</Body>}
    {!busy && <div style={{ overflowX: 'auto' }}><table className="mdb">
      <thead><tr><th>Projeto / Cluster</th><th>Tier</th><th>CPU média</th><th>Pior p95</th><th>Cobertura</th><th>USD/Mês (est.)</th><th>Recomendação</th></tr></thead>
      <tbody>{[...rows].sort((a, b) => b.cost_usd - a.cost_usd).map(c => <tr key={`${c.project_id}/${c.cluster}`}>
        <td>{c.project}<div className="mono" style={{ color: 'var(--accent)' }}>{c.cluster}</div></td>
        <td>{c.tier}</td><td>{pct(c.cpu)}</td><td>{pct(c.worst_node_p95)}</td>
        <td>{c.status === 'paused' ? 'pausado' : `${c.coverage_pct}% · ${c.measured_nodes}/${c.expected_nodes} nós`}</td>
        <td>${fmt(c.cost_usd)}</td><td><Badge variant={VAR[c.color]}>{c.verdict}</Badge>
          <details style={{ marginTop: 8 }}><summary>Evidências por nó</summary>
            {c.reasons.map((r, i) => <p key={i}>{r}</p>)}
            <table className="mdb"><thead><tr><th>Nó / papel atual</th><th>Média 24h</th><th>p95 24h</th><th>p95 30min</th><th>Cobertura</th></tr></thead>
              <tbody>{c.nodes.map(n => <tr key={n.process_id}><td>{n.alias}<div>{n.role}</div>{n.error && <div>Coleta indisponível</div>}</td>
                <td>{pct(n.cpu_24h?.avg)}</td><td>{pct(n.cpu_24h?.p95)}</td><td>{pct(n.cpu_recent?.p95)}</td><td>{pct(n.coverage_pct)}</td></tr>)}</tbody></table>
            {c.nodes.map(n => <p key={n.process_id}><strong>{n.alias.split('.')[0]}</strong> · snapshot: RAM {n.snapshot?._memory_available ? pct(n.snapshot.mem_pct) : '—'} · disco {n.snapshot?._disk_available ? pct(n.snapshot.disk_pct) : '—'} · IOPS R/W {n.snapshot?._disk_available ? `${n.snapshot.disk_iops_read}/${n.snapshot.disk_iops_write}` : '—'} · conexões {n.snapshot?._raw?.CONNECTIONS ?? '—'} · scanned/returned {n.snapshot?._raw?.QUERY_TARGETING_SCANNED_OBJECTS_PER_RETURNED ?? '—'}</p>)}
          </details>
          {c.status !== 'paused' && <Button size="small" onClick={() => analyze(c)} disabled={busyAI}>Analisar com MCP + LLM</Button>}
        </td>
      </tr>)}</tbody>
    </table></div>}
    {(busyAI || analysis) && <>
      <Section title={`Relatório · ${analysisCluster}`} sub="Evidências reais via MCP · sugestões do modelo sujeitas a validação" />
      {busyAI && <Body>{phase} · {elapsed}s <Button onClick={() => request.current?.abort()}>Interromper</Button></Body>}
      {tools.length > 0 && <Body>{tools.map(t => `${t.running ? '⏳' : t.ok ? '✓' : '⚠'} ${t.label}`).join(' → ')}</Body>}
      <div className="panel" style={{ padding: 18 }}><ReactMarkdown remarkPlugins={[remarkGfm]}>{analysis}</ReactMarkdown></div>
      {analysis && !busyAI && !error && <Button onClick={() => downloadReport(analysisCluster, analysis).catch(e => setError(e.message))}>Exportar relatório PDF</Button>}
    </>}
  </>
}
