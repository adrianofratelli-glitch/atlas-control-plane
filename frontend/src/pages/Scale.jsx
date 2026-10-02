import { useClusterSelection } from '../cluster-context.jsx'
import { useState, useEffect } from 'react'
import { H1, Body } from '@leafygreen-ui/typography'
import Button from '@leafygreen-ui/button'
import Banner from '@leafygreen-ui/banner'
import Badge from '@leafygreen-ui/badge'
import Card from '@leafygreen-ui/card'
import { KpiGrid, Kpi, Section, Empty } from '../components.jsx'
import { getScaling, getLiveMetrics, scaleCluster } from '../api.js'
import { ClusterPicker } from './_picker.jsx'

export default function Scale({ clusters, config }) {
  const [sel, setSel] = useClusterSelection()
  const [rec, setRec] = useState(null)
  const [live, setLive] = useState(null)
  const [liveError, setLiveError] = useState('')
  const [newTier, setNewTier] = useState(sel?.tier)
  const [msg, setMsg] = useState(null)
  const [confirm, setConfirm] = useState(false)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!sel) return
    let cancelled = false, liveTimer, recTimer, livePending = false
    setRec(null); setLive(null); setNewTier(sel.tier); setMsg(null); setConfirm(false); setLoading(true); setLiveError('')
    const pollLive = async () => {
      if (cancelled || document.hidden || livePending) return
      livePending = true
      try {
        if (!document.hidden) {
          const result = await getLiveMetrics(sel.project_id, sel.cluster_name, sel.tier)
          if (!cancelled) { setLive(result); setLiveError('') }
        }
      } catch (e) { if (!cancelled) setLiveError(e?.response?.data?.detail || 'Não foi possível atualizar as métricas.') }
      finally { livePending = false; if (!cancelled) setLoading(false) }
    }
    const pollRecommendation = async () => {
      try { const result = await getScaling(sel.project_id, sel.cluster_name, sel.tier); if (!cancelled) setRec(result) }
      catch { /* Live metrics still work if historical analysis is unavailable. */ }
      finally { if (!cancelled) recTimer = setTimeout(pollRecommendation, 60000) }
    }
    pollLive(); liveTimer = setInterval(pollLive, 5000); pollRecommendation()
    return () => { cancelled = true; clearInterval(liveTimer); clearTimeout(recTimer) }
  }, [sel?.project_id, sel?.cluster_name, sel?.tier])

  const primary = live?.nodes.find(n => n.role === 'REPLICA_PRIMARY')?.snapshot
  const cpuNodes = live?.nodes.filter(n => n.snapshot?._raw?.SYSTEM_NORMALIZED_CPU_USER != null && n.snapshot?._raw?.SYSTEM_NORMALIZED_CPU_KERNEL != null) || []
  const cpuMean = cpuNodes.length ? Math.round(cpuNodes.reduce((sum, n) => sum + n.snapshot.cpu_pct, 0) / cpuNodes.length * 10) / 10 : null
  const hottest = [...cpuNodes].sort((a, b) => b.snapshot.cpu_pct - a.snapshot.cpu_pct)[0]

  const usdBrl = config.usd_brl
  const pricing = config.pricing || {}
  const tiers = config.tiers?.dedicated || []

  if (!sel) return (
    <>
      <div className="page-head"><H1 style={{ color: 'var(--text-pri)' }}>Scale</H1></div>
      <Empty icon="📈" title="Nenhum cluster encontrado" hint="Verifique as credenciais do Atlas no servidor (.env) e recarregue a página." />
    </>
  )

  const curUsd = pricing[sel.tier] || 0
  const newUsd = pricing[newTier] || 0
  const deltaUsd = newUsd - curUsd
  const curIdx = tiers.indexOf(sel.tier), newIdx = tiers.indexOf(newTier)
  const direction = newIdx > curIdx ? '⬆️ Scale UP' : newIdx < curIdx ? '⬇️ Scale DOWN' : ''

  const doScale = async () => {
    setConfirm(false)
    try { const r = await scaleCluster(sel.project_id, sel.cluster_name, newTier); setMsg({ ok: true, t: `Scaling iniciado! Status: ${r.state}. O cluster entrará em UPDATING por alguns minutos.` }) }
    catch (e) { setMsg({ ok: false, t: e?.response?.data?.detail || e.message }) }
  }

  const recColor = rec?.severity === 'high' ? 'danger' : rec?.severity === 'med' ? 'warning' : 'success'
  return (
    <>
      <div className="page-head"><H1 style={{ color: 'var(--text-pri)' }}>Scale</H1></div>
      <div className="row" style={{ marginBottom: 16 }}>
        <ClusterPicker clusters={clusters} value={sel} onChange={setSel} />
      </div>

      <KpiGrid>
        <Kpi label="Cluster" value={sel.cluster_name} color="#00c2eb" />
        <Kpi label="Tier Atual" value={sel.tier} color="#00A35C" />
        <Kpi label="Região" value={sel.region_pretty} color="#9ea2a1" />
        <Kpi label="Custo Est./Mês" value={`R$ ${sel.cost_brl.toLocaleString('pt-BR')}`} delta={`≈ USD ${sel.cost_usd.toLocaleString('pt-BR')} · tabela us-east-1`} />
      </KpiGrid>

      {/* Native auto-scaling status — a customer WILL ask "doesn't Atlas do this by itself?" */}
      <div className="row" style={{ marginBottom: 18, gap: 8, flexWrap: 'wrap' }}>
        <Badge variant={sel.autoscale_compute ? 'green' : 'lightgray'}>
          Auto-scaling compute: {sel.autoscale_compute ? `ON · ${sel.autoscale_min}–${sel.autoscale_max}` : 'OFF'}
        </Badge>
        <Badge variant={sel.autoscale_disk ? 'green' : 'lightgray'}>
          Auto-scaling disco: {sel.autoscale_disk ? 'ON' : 'OFF'}
        </Badge>
        <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>
          {sel.autoscale_compute
            ? 'O Atlas já escala este cluster automaticamente — este painel oferece uma análise independente por nó.'
            : 'O Atlas oferece auto-scaling nativo de compute e disco — este painel oferece sinais para uma decisão de capacidade.'}
        </span>
      </div>

      {/* ── Key metrics that govern scaling: CPU · Memory · Storage ── */}
      <Section title="Métricas de Scaling" />
      {live?.fetched_at && <Body style={{ marginBottom: 12 }}>Última consulta à API: {new Date(live.fetched_at).toLocaleTimeString('pt-BR')} · {live.nodes.length} nó(s).</Body>}
      {liveError && <Banner variant="warning">{liveError} A última coleta permanece identificada pelo horário.</Banner>}
      {loading && <Body style={{ color: 'var(--text-muted)' }}>Coletando métricas dos nós…</Body>}
      {live && !live.nodes.length && <Banner variant="info">Sem processos ativos para este cluster.</Banner>}
      {live?.nodes.length > 0 && <>
        <div className="responsive-four-col" style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 14, marginBottom: 18 }}>
          <MetricBar label="CPU média dos nós" pct={cpuMean} sub={hottest ? `Maior CPU: ${hottest.snapshot.cpu_pct}% · ${hottest.alias.split('.')[0]}` : 'CPU indisponível'} warn={75} crit={90} />
          <MetricBar label="Memória do primary" pct={primary?._memory_available ? primary.mem_pct : null} sub={primary?._memory_available ? `${primary.memory_used_gb}/${primary.mem_total_gb} GB · uso não prova pressão de cache` : 'Indisponível'} />
          <MetricBar label="Storage do primary" pct={primary?._disk_available ? primary.disk_pct : null} sub="Percentual do disco ocupado" warn={70} crit={85} />
          <MetricBar label="Conexões do primary" pct={primary?._raw?.CONNECTIONS != null ? primary.connections / (live.connection_limit || 3000) * 100 : null} sub={primary?._raw?.CONNECTIONS != null ? `${primary.connections} conexões` : 'Indisponível'} warn={60} crit={80} />
        </div>
        <div style={{ overflowX: 'auto' }}><table className="mdb"><thead><tr><th>Nó / papel</th><th>CPU atual</th><th>RAM</th><th>Disco</th><th>IOPS R/W</th><th>Conexões</th><th>Amostra CPU</th></tr></thead>
          <tbody>{live.nodes.map(n => <tr key={n.alias}><td>{n.alias.split('.')[0]}<div>{n.role}</div></td><td>{n.snapshot?._raw?.SYSTEM_NORMALIZED_CPU_USER != null ? `${n.snapshot.cpu_pct}%` : '—'}</td><td>{n.snapshot?._memory_available ? `${n.snapshot.mem_pct}%` : '—'}</td><td>{n.snapshot?._disk_available ? `${n.snapshot.disk_pct}%` : '—'}</td><td>{n.snapshot?._disk_available ? `${n.snapshot.disk_iops_read}/${n.snapshot.disk_iops_write}` : '—'}</td><td>{n.snapshot?._raw?.CONNECTIONS ?? '—'}</td><td>{n.snapshot?.cpu_observed_at ? new Date(n.snapshot.cpu_observed_at).toLocaleTimeString('pt-BR') : '—'}</td></tr>)}</tbody></table></div>
      </>}

      {/* ── Recommendation: why to scale (or not) ── */}
      <Section title="Recomendação Inteligente" sub="baseada em CPU · memória · storage · conexões reais" />
      {rec && rec.headline && (
        <Banner variant={recColor} style={{ marginBottom: 8 }}>
          <b>{rec.headline}</b>
          <ul style={{ margin: '8px 0 0', paddingLeft: 18 }}>
            {rec.reasons.map((r, i) => <li key={i} style={{ margin: '4px 0' }}>{r.replace(/\*\*/g, '')}</li>)}
          </ul>
        </Banner>
      )}
      {rec && rec.action === 'ok' && (
        <Body style={{ color: 'var(--text-muted)', fontSize: 13 }}>
          ✅ Sem necessidade imediata de scaling — você ainda pode simular cenários abaixo para planejar crescimento.
        </Body>
      )}

      {/* ── Tier simulator with cost ── */}
      <Section title="Simular Mudança de Tier" />
      <Card className="panel" darkMode>
        <div className="row" style={{ alignItems: 'flex-end', gap: 18 }}>
          <div>
            <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>Novo tier</div>
            <select className="mono" value={newTier} onChange={e => { setNewTier(e.target.value); setConfirm(false); setMsg(null) }}
              style={{ background: 'var(--bg-secondary)', color: 'var(--text-pri)', border: '1px solid var(--border-accent)', borderRadius: 6, padding: '9px 14px', fontSize: 15 }}>
              {tiers.map(t => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          {newTier !== sel.tier && (
            <>
              <div style={{ fontSize: 22, color: 'var(--text-muted)' }}>→</div>
              <div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>{direction}</div>
                <div className="mono" style={{ fontSize: 20, fontWeight: 700, color: 'var(--text-pri)' }}>
                  R$ {Math.round(newUsd * usdBrl).toLocaleString('pt-BR')}<span style={{ fontSize: 12, color: 'var(--text-muted)' }}>/mês</span>
                </div>
              </div>
              <div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6 }}>Variação</div>
                <div className="mono" style={{ fontSize: 18, fontWeight: 700, color: deltaUsd >= 0 ? '#ef4444' : '#00ED64' }}>
                  {deltaUsd >= 0 ? '+' : ''}R$ {Math.round(deltaUsd * usdBrl).toLocaleString('pt-BR')}
                  <span style={{ fontSize: 11, color: 'var(--text-muted)' }}> ({deltaUsd >= 0 ? '+' : ''}USD {deltaUsd.toLocaleString('pt-BR')})</span>
                </div>
              </div>
              <div className="spacer" />
              {!confirm
                ? <Button variant="primary" onClick={() => setConfirm(true)}>🚀 Executar Scaling</Button>
                : <div className="row" style={{ gap: 8 }}>
                    <Button variant="danger" onClick={doScale}>Confirmar {sel.tier} → {newTier}</Button>
                    <Button variant="default" onClick={() => setConfirm(false)}>Cancelar</Button>
                  </div>}
            </>
          )}
        </div>
        {confirm && (
          <Banner variant="warning" style={{ marginTop: 12 }}>
            ⚠️ Isso executa um <b>PATCH real</b> no cluster <b>{sel.cluster_name}</b> ({sel.tier} → {newTier}) via Admin API. Confirme para prosseguir.
          </Banner>
        )}
        {newIdx < curIdx && newIdx >= 0 && (
          <div style={{ fontSize: 11, color: '#ff4f00', marginTop: 10 }}>
            ⚠️ Scale down exige que dados e oplog caibam no storage do tier menor — o Atlas bloqueia a operação se não couberem.
          </div>
        )}
        <Banner variant="info" style={{ marginTop: 16 }}>
          O scaling é um <b>rolling restart nó a nó</b> — sem downtime perceptível para aplicações com <b>retryable writes</b>; há um failover de primário de alguns segundos ao final.
        </Banner>
      </Card>
      {msg && <Banner variant={msg.ok ? 'success' : 'danger'} style={{ marginTop: 14 }}>{msg.t}</Banner>}
    </>
  )
}

// Metric bar with color by severity (green → yellow → red)
function MetricBar({ label, pct, sub, warn = 75, crit = 90 }) {
  const v = Math.max(0, Math.min(100, pct || 0))
  const color = pct == null ? 'var(--text-muted)' : v >= crit ? '#F87171' : v >= warn ? '#FACC15' : '#00ED64'
  return (
    <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border-subtle)',
                  borderTop: `3px solid ${color}`, borderRadius: '0 0 8px 8px', padding: '14px 16px' }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '1.2px',
                    color: 'var(--text-muted)', marginBottom: 8 }}>{label}</div>
      <div className="mono" style={{ fontSize: 22, fontWeight: 700, color, lineHeight: 1 }}>{pct == null ? '—' : `${Math.round(v * 10) / 10}%`}</div>
      <div style={{ height: 6, background: 'var(--bg-secondary)', borderRadius: 3, overflow: 'hidden', margin: '8px 0 6px' }}>
        <div style={{ width: `${v}%`, height: '100%', background: color }} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', fontFamily: "'Source Code Pro',ui-monospace,monospace" }}>{sub}</div>
    </div>
  )
}
