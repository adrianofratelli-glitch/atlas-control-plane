import { useClusterSelection } from '../cluster-context.jsx'
import { useState, useMemo, Fragment } from 'react'
import { H1 } from '@leafygreen-ui/typography'
import Button from '@leafygreen-ui/button'
import Banner from '@leafygreen-ui/banner'
import Badge from '@leafygreen-ui/badge'
import { KpiGrid, Kpi, Section, Empty } from '../components.jsx'
import { getSlow, explainQuery } from '../api.js'
import { ClusterPicker } from './_picker.jsx'
import QueryDetails from '../components/QueryDetails.jsx'

const PLAN = { COLLSCAN: '🔴 COLLSCAN', IXSCAN: '🟢 IXSCAN', FETCH: '🟡 FETCH', SORT: '🟠 SORT', IDHACK: '🟢 IDHACK' }
const WRITE_OPS = ['insert', 'update', 'remove', 'delete', 'findAndModify']

function classify(attr) {
  const t = (attr.type || '').toLowerCase()
  const cmd = attr.command || {}
  const op = t || Object.keys(cmd)[0] || 'query'
  const isWrite = WRITE_OPS.some(w => op.includes(w))
  return { op, kind: isWrite ? 'write' : 'read' }
}

export default function Profiler({ clusters, config }) {
  const [sel, setSel] = useClusterSelection()
  const [rows, setRows] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [sort, setSort] = useState('dur')       // dur | count
  const [filter, setFilter] = useState('all')   // all | read | write | collscan
  const [open, setOpen] = useState(null)        // key of the expanded row (stable across sort/filter)
  const [expl, setExpl] = useState(null)        // { key, busy, data, err } — real explain result

  const load = async () => {
    setBusy(true); setErr(null); setRows(null); setOpen(null); setExpl(null)
    try {
      const data = await getSlow(sel.project_id, sel.cluster_name)
      // Group by shape (namespace + plan + operation) to count executions
      const map = {}
      for (const q of (data.slowQueries || [])) {
        let attr = {}
        try { attr = JSON.parse(q.line || '{}').attr || {} } catch {}
        const { op, kind } = classify(attr)
        const planRaw = attr.planSummary || '—'
        let plan = planRaw
        for (const k of Object.keys(PLAN)) if (String(planRaw).includes(k)) { plan = PLAN[k]; break }
        const ns = q.namespace || 'N/A'
        const key = `${ns}|${plan}|${op}`
        const cmd = attr.command || {}
        if (!map[key]) map[key] = {
          ns, plan, planRaw, op, kind, count: 0, totalDur: 0, maxDur: 0,
          docs: 0, keys: 0, ret: 0, yields: 0,
          filter: cmd.filter || cmd.q || cmd.pipeline || {},
          queryHash: attr.queryHash || '—',
        }
        const r = map[key]
        r.count++; r.totalDur += attr.durationMillis || 0
        r.maxDur = Math.max(r.maxDur, attr.durationMillis || 0)
        r.docs = Math.max(r.docs, attr.docsExamined || 0)
        r.keys = Math.max(r.keys, attr.keysExamined || 0)
        r.ret = Math.max(r.ret, attr.nreturned || 0)
        r.yields = Math.max(r.yields, attr.numYields || 0)
      }
      setRows(Object.values(map).map(r => ({ ...r, avgDur: Math.round(r.totalDur / r.count) })))
    } catch (e) { setErr(e?.response?.data?.detail || e.message) }
    finally { setBusy(false) }
  }

  const runExplain = async (key, r) => {
    setExpl({ key, busy: true })
    try {
      setExpl({ key, busy: false, data: await explainQuery(r.ns, r.filter, sel.project_id, sel.cluster_name) })
    } catch (e) {
      setExpl({ key, busy: false, err: e?.response?.data?.detail || e.message })
    }
  }

  const filtered = useMemo(() => {
    if (!rows) return []
    let r = rows
    if (filter === 'read') r = r.filter(x => x.kind === 'read')
    if (filter === 'write') r = r.filter(x => x.kind === 'write')
    if (filter === 'collscan') r = r.filter(x => String(x.plan).includes('COLLSCAN'))
    return [...r].sort((a, b) => sort === 'count' ? b.count - a.count : b.maxDur - a.maxDur)
  }, [rows, sort, filter])

  const collscans = rows?.filter(r => String(r.plan).includes('COLLSCAN')).reduce((s, r) => s + r.count, 0) || 0
  const totalExec = rows?.reduce((s, r) => s + r.count, 0) || 0
  const worst = rows?.length ? Math.max(...rows.map(r => r.maxDur)) : 0

  return (
    <>
      <div className="page-head"><H1 style={{ color: 'var(--text-pri)' }}>Query Profiler</H1></div>
      <div className="row" style={{ marginBottom: 18 }}>
        <ClusterPicker clusters={clusters} value={sel} onChange={setSel} />
        <Button variant="primary" onClick={load} disabled={busy}>{busy ? 'Buscando…' : '🔍 Carregar Slow Queries'}</Button>
      </div>

      {err && <Banner variant="danger">{err}</Banner>}
      {!rows && !err && <Empty icon="🔍" title="Investigue as queries lentas" hint="Carregue as slow queries para ver shapes agrupados por execução, tipo leitura/escrita, plano, e rodar explain real." />}
      {rows && rows.length === 0 && <Banner variant="success">Nenhuma slow query em {sel.cluster_name}.</Banner>}
      {rows && rows.length > 0 && (
        <>
          <KpiGrid>
            <Kpi label="Query Shapes" value={rows.length} color="#f97316" />
            <Kpi label="Execuções Lentas" value={totalExec.toLocaleString('pt-BR')} color="#06b6d4" />
            <Kpi label="COLLSCANs" value={collscans} delta={collscans ? 'sem índice' : '✓ nenhum'} color={collscans ? '#FF4444' : '#00ED64'} />
            <Kpi label="Pior Latência" value={`${worst.toLocaleString('pt-BR')}ms`} color="#ef4444" />
          </KpiGrid>

          {/* Filters + sorting */}
          <div className="row" style={{ marginBottom: 12 }}>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>Filtrar:</span>
            {['all', 'read', 'write', 'collscan'].map(f => (
              <button key={f} onClick={() => setFilter(f)} className="chip" data-active={filter === f}>
                {f === 'all' ? 'Todas' : f === 'read' ? '📖 Leitura' : f === 'write' ? '✏️ Escrita' : '🔴 COLLSCAN'}
              </button>
            ))}
            <span className="spacer" />
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>Ordenar:</span>
            <button onClick={() => setSort('dur')} className="chip" data-active={sort === 'dur'}>Latência</button>
            <button onClick={() => setSort('count')} className="chip" data-active={sort === 'count'}>Execuções</button>
          </div>

          <table className="mdb">
            <thead><tr><th>Namespace</th><th>Tipo</th><th>Plano</th><th>Execuções</th><th>Latência (máx/méd)</th><th>Docs Exam.</th><th></th></tr></thead>
            <tbody>
              {filtered.map((r) => {
                const key = `${r.ns}|${r.planRaw}|${r.op}`
                const ratio = r.ret > 0 ? Math.round(r.docs / r.ret) : (r.docs > 0 ? r.docs : 0)
                const ratioBad = ratio >= 100
                return (
                  <Fragment key={key}>
                    <tr>
                      <td className="mono" style={{ color: '#00ED64' }}>
                        <div title={r.ns} style={{ maxWidth: 230, overflow: 'hidden',
                             textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.ns}</div>
                      </td>
                      <td>
                        <Badge variant={r.kind === 'write' ? 'yellow' : 'blue'}>
                          <span title={r.op} style={{ display: 'inline-block', maxWidth: 100, overflow: 'hidden',
                                  textOverflow: 'ellipsis', whiteSpace: 'nowrap', verticalAlign: 'bottom' }}>
                            {r.kind === 'write' ? '✏️ ' : '📖 '}{r.op}
                          </span>
                        </Badge>
                      </td>
                      <td>
                        <div title={r.planRaw} style={{ maxWidth: 130, overflow: 'hidden',
                             textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.plan}</div>
                      </td>
                      <td className="mono">{r.count.toLocaleString('pt-BR')}×</td>
                      <td className="mono">{r.maxDur.toLocaleString('pt-BR')} / {r.avgDur.toLocaleString('pt-BR')}ms</td>
                      <td className="mono" style={{ color: 'var(--text-muted)' }}>{r.docs.toLocaleString('pt-BR')}</td>
                      <td><Button size="xsmall" onClick={() => { setOpen(open === key ? null : key); setExpl(null) }}>{open === key ? 'fechar' : '🔬 plano'}</Button></td>
                    </tr>
                    {open === key && (
                      <tr><td colSpan={7} style={{ background: 'var(--bg-secondary)', padding: 16 }}>
                        <div style={{ fontSize: 11, color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 10 }}>Plano de execução capturado (slow query log)</div>
                        <div className="mono" style={{ fontSize: 12, color: '#C3E7DD', display: 'flex', gap: 28, flexWrap: 'wrap', marginBottom: 12 }}>
                          <span>plano: <b style={{ color: r.planRaw.includes('COLLSCAN') ? '#ef4444' : '#00ED64' }}>{r.planRaw}</b></span>
                          <span>docs examinados: <b>{r.docs.toLocaleString('pt-BR')}</b></span>
                          <span>keys examinados: <b>{r.keys.toLocaleString('pt-BR')}</b></span>
                          <span>retornados: <b>{r.ret.toLocaleString('pt-BR')}</b></span>
                          <span>yields: {r.yields}</span>
                          <span>queryHash: {r.queryHash}</span>
                        </div>
                        <div style={{ marginBottom: 10 }}>
                          <Badge variant={ratioBad ? 'red' : ratio > 10 ? 'yellow' : 'green'}>
                            Query Targeting (aprox.): {ratio.toLocaleString('pt-BR')} docs escaneados por documento retornado
                          </Badge>
                          {ratioBad && <span style={{ fontSize: 12, color: '#ef4444', marginLeft: 10 }}>← índice provavelmente faltando</span>}
                        </div>
                        <QueryDetails
                          operation={r.op}
                          namespace={r.ns}
                          query={r.filter}
                          explain={expl?.key === key ? expl.data : {
                            stage: r.planRaw, docs_examined: r.docs, keys_examined: r.keys,
                            n_returned: r.ret, query_hash: r.queryHash,
                          }}
                          label="Ver query e plano capturado"
                        />
                        {config.mongodb && sel.is_uri_target && r.kind === 'read' && !Array.isArray(r.filter) && (
                          <div style={{ marginTop: 12 }}>
                            <Button size="xsmall" disabled={expl?.busy} onClick={() => runExplain(key, r)}>
                              {expl?.busy ? '⏳ Executando…' : "▶ Rodar explain('executionStats') real"}
                            </Button>
                            {expl?.key === key && expl.err && (
                              <Banner variant="danger" style={{ marginTop: 8 }}>{expl.err}</Banner>
                            )}
                            {expl?.key === key && expl.data && (
                              <div className="mono" style={{ fontSize: 12, color: '#C3E7DD', display: 'flex', gap: 24, flexWrap: 'wrap', marginTop: 10 }}>
                                <span>stage: <b style={{ color: String(expl.data.stage).includes('COLLSCAN') ? '#ef4444' : '#00ED64' }}>{expl.data.stage}</b></span>
                                <span>docs examinados: <b>{expl.data.docs_examined}</b></span>
                                <span>keys examinados: <b>{expl.data.keys_examined}</b></span>
                                <span>retornados: <b>{expl.data.n_returned}</b></span>
                                <span>tempo: <b>{expl.data.exec_ms}ms</b></span>
                                <span>índice: <b>{expl.data.index_used}</b></span>
                              </div>
                            )}
                          </div>
                        )}
                      </td></tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
          {collscans > 0 && <Banner variant="info" style={{ marginTop: 14 }}>💡 As {collscans} execuções 🔴 COLLSCAN varrem a collection inteira — crie índices na aba Performance Advisor.</Banner>}
        </>
      )}
    </>
  )
}
