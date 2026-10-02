import { useClusterSelection } from '../cluster-context.jsx'
import { useState } from 'react'
import { H1, Body } from '@leafygreen-ui/typography'
import Button from '@leafygreen-ui/button'
import Banner from '@leafygreen-ui/banner'
import Card from '@leafygreen-ui/card'
import { Section, Empty } from '../components.jsx'
import { getPA, createIndex, streamAnalyze, downloadReport } from '../api.js'
import { ClusterPicker } from './_picker.jsx'
import QueryDetails from '../components/QueryDetails.jsx'

export default function PerformanceAdvisor({ clusters, config }) {
  const [sel, setSel] = useClusterSelection()
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [busyAI, setBusyAI] = useState(false)
  const [err, setErr] = useState(null)
  const [analysis, setAnalysis] = useState('')
  const [idxMsg, setIdxMsg] = useState(null)   // { i, ok, text } — createIndex result per card

  const load = async () => {
    setBusy(true); setErr(null); setData(null); setAnalysis(''); setIdxMsg(null)
    try { setData(await getPA(sel.project_id, sel.cluster_name)) }
    catch (e) { setErr(e?.response?.data?.detail || e.message) }
    finally { setBusy(false) }
  }

  const analyze = async () => {
    setAnalysis(''); setErr(null); setBusyAI(true)
    try {
      for await (const chunk of streamAnalyze(sel.project_id, sel.cluster_name)) setAnalysis(a => a + chunk)
    } catch (e) { setErr(e.message) }
    finally { setBusyAI(false) }
  }

  const runIndex = async (i, ns, indexKeys) => {
    setIdxMsg({ i, ok: true, text: '⏳ Criando índice…' })
    try {
      const r = await createIndex(ns, indexKeys, sel.project_id, sel.cluster_name)
      setIdxMsg({ i, ok: !String(r.result).startsWith('❌'), text: r.result })
    } catch (e) {
      setIdxMsg({ i, ok: false, text: e?.response?.data?.detail || e.message })
    }
  }

  const suggestions = data?.suggestedIndexes || []
  return (
    <>
      <div className="page-head"><H1 style={{ color: 'var(--text-pri)' }}>Performance Advisor</H1></div>
      <div className="row" style={{ marginBottom: 18 }}>
        <ClusterPicker clusters={clusters} value={sel} onChange={setSel} />
        <Button variant="primary" onClick={load} disabled={busy}>{busy ? 'Consultando…' : '🔍 Buscar Recomendações'}</Button>
      </div>

      {err && <Banner variant="danger">{err}</Banner>}
      {!data && !err && <Empty icon="⚡" title="Analise os índices de um cluster" hint="Selecione o cluster e clique em Buscar Recomendações para o Performance Advisor sugerir índices com base nos padrões de acesso reais." />}

      {data && suggestions.length === 0 && <Banner variant="success">Nenhuma recomendação do Advisor para {sel.cluster_name}. Consulte as queries e métricas antes de concluir sobre a saúde do cluster.</Banner>}
      {data && suggestions.length > 0 && (
        <>
          <Banner variant="warning">{suggestions.length} índice(s) sugerido(s) para {sel.cluster_name}</Banner>
          <Section title="Índices Sugeridos" />
          {suggestions.map((idx, i) => {
            const ns = idx.namespace || 'N/A'
            const fields = (idx.index || []).map(k => `"${Object.keys(k)[0]}": ${Object.values(k)[0]}`).join(', ')
            const cmd = `db.${ns.split('.').pop()}.createIndex({ ${fields} })`
            return (
              <Card className="panel" key={i} darkMode style={{ marginBottom: 12 }}>
                <Body weight="medium">
                  #{i + 1} · {ns} ·{' '}
                  <span title="Score interno do Performance Advisor — quanto maior, maior o ganho esperado ao criar o índice">
                    impacto estimado {Math.round((idx.weight || 0) * 100) / 100}
                  </span>
                </Body>
                <QueryDetails operation="createIndex" namespace={ns} query={cmd} note="recomendação do Atlas Performance Advisor" />
                {config.mongodb && sel.is_uri_target && (
                  <Button size="small" onClick={() => runIndex(i, ns, idx.index)}>▶ Executar Índice</Button>
                )}
                {config.mongodb && !sel.is_uri_target && (
                  <div style={{ fontSize: 11, color: '#ff4f00' }}>
                    ⚠️ Execução desabilitada — o MONGODB_URI do servidor aponta para outro cluster.
                  </div>
                )}
                {idxMsg?.i === i && (
                  <Banner variant={idxMsg.ok ? 'success' : 'danger'} style={{ marginTop: 10 }}>{idxMsg.text}</Banner>
                )}
              </Card>
            )
          })}
          <div className="row" style={{ marginTop: 8, gap: 10 }}>
            <Button variant="primaryOutline" onClick={analyze} disabled={busyAI}>
              {busyAI ? '🤖 Analisando…' : '🤖 Analisar com Claude'}
            </Button>
            {analysis && !busyAI && (
              <Button variant="default" onClick={() => downloadReport(sel.cluster_name, analysis).catch(e => setErr(e.message))}>
                📄 Baixar Relatório PDF
              </Button>
            )}
          </div>
          {analysis && <Card className="panel" darkMode style={{ marginTop: 12 }}><div style={{ whiteSpace: 'pre-wrap' }}>{analysis}</div></Card>}
        </>
      )}
    </>
  )
}
