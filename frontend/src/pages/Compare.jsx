import { useState } from 'react'
import { H1, Body } from '@leafygreen-ui/typography'
import Button from '@leafygreen-ui/button'
import Banner from '@leafygreen-ui/banner'
import { Section, Empty } from '../components.jsx'
import { getHealth } from '../api.js'
import { clusterKey } from './_picker.jsx'

export default function Compare({ clusters }) {
  const [a, setA] = useState(clusters[0])
  const [b, setB] = useState(clusters[1] || clusters[0])
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)

  const cmp = async () => {
    setBusy(true); setErr(null)
    try {
      const [ha, hb] = await Promise.all([
        getHealth(a.project_id, a.cluster_name, a.status, a.mongo_version),
        getHealth(b.project_id, b.cluster_name, b.status, b.mongo_version),
      ])
      setData({ a: { ...a, ...ha }, b: { ...b, ...hb } })
    } catch (e) {
      setErr(e?.response?.data?.detail || e.message)
    } finally { setBusy(false) }
  }

  if (clusters.length < 2) return <><div className="page-head"><H1>Compare</H1></div><Empty icon="📊" title="São necessários 2 clusters" hint="Esta organização tem apenas um cluster." /></>

  const Sel = ({ v, set, label }) => (
    <div>
      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginBottom: 6 }}>{label}</div>
      <select className="mono" value={clusterKey(v)} onChange={e => set(clusters.find(c => clusterKey(c) === e.target.value))}
        style={{ background: 'var(--bg-card)', color: 'var(--text-pri)', border: '1px solid var(--border-accent)', borderRadius: 6, padding: '8px 12px', minWidth: 220 }}>
        {clusters.map(c => <option key={clusterKey(c)} value={clusterKey(c)}>{c.project_name} / {c.cluster_name}</option>)}
      </select>
    </div>
  )

  const sameCluster = clusterKey(a) === clusterKey(b)

  // metric, valueA, valueB, betterWhen ('high'|'low'|null)
  // Context rows (cost, tier, region) stay out of the win count — a cheaper
  // tier isn't "better", it's just smaller.
  const metrics = data ? [
    ['Health Score', data.a.score, data.b.score, 'high', v => v == null ? '—' : `${v}/100`],
    ['Grade', data.a.grade, data.b.grade, null, v => v],
    ['PA Sugestões', data.a.n_pa, data.b.n_pa, 'low', v => v],
    ['Slow Queries', data.a.n_sq, data.b.n_sq, 'low', v => v],
    ['Custo USD/Mês (est.)', data.a.cost_usd, data.b.cost_usd, null, v => `$${v}`],
    ['Tier', data.a.tier, data.b.tier, null, v => v],
    ['Região', data.a.region_pretty, data.b.region_pretty, null, v => v],
    ['MongoDB', data.a.mongo_version, data.b.mongo_version, 'high', v => v],
  ] : []

  // Compares versions segment by segment ("7.0.28" vs "8.0.5") — parseFloat
  // would drop the patch and tie "7.0.2" with "7.0.28"
  const cmpVersion = (va, vb) => {
    const sa = String(va).split('.').map(Number), sb = String(vb).split('.').map(Number)
    for (let i = 0; i < Math.max(sa.length, sb.length); i++) {
      const d = (sa[i] || 0) - (sb[i] || 0)
      if (d !== 0) return d
    }
    return 0
  }

  const winner = (va, vb, better) => {
    if (better === null || va === vb || va == null || vb == null) return 0
    const isVersion = v => /^\d+(\.\d+)+$/.test(String(v))
    let diff
    if (isVersion(va) && isVersion(vb)) {
      diff = cmpVersion(va, vb)
    } else {
      const na = parseFloat(va), nb = parseFloat(vb)
      const a_ = isNaN(na) ? va : na, b_ = isNaN(nb) ? vb : nb
      diff = a_ > b_ ? 1 : a_ < b_ ? -1 : 0
    }
    if (diff === 0) return 0
    if (better === 'high') return diff > 0 ? -1 : 1
    return diff < 0 ? -1 : 1   // lower is better
  }

  // Summary: who won more metrics
  let scoreA = 0, scoreB = 0
  metrics.forEach(([, va, vb, better]) => { const w = winner(va, vb, better); if (w < 0) scoreA++; if (w > 0) scoreB++ })

  return (
    <>
      <div className="page-head"><H1 style={{ color: 'var(--text-pri)' }}>Comparar Clusters</H1></div>
      <div className="row" style={{ marginBottom: 18, alignItems: 'flex-end' }}>
        <Sel v={a} set={setA} label="🔵 Cluster A" />
        <Sel v={b} set={setB} label="🟠 Cluster B" />
        <Button variant="primary" onClick={cmp} disabled={busy || sameCluster}>{busy ? 'Comparando…' : '🔍 Comparar'}</Button>
      </div>

      {sameCluster && <Banner variant="info" style={{ marginBottom: 16 }}>Selecione dois clusters diferentes para comparar.</Banner>}
      {err && <Banner variant="danger" style={{ marginBottom: 16 }}>{err}</Banner>}

      {data && (
        <>
          <Banner variant={scoreA === scoreB ? 'info' : 'success'} style={{ marginBottom: 16 }}>
            {scoreA === scoreB
              ? `Empate técnico — ${data.a.cluster_name} e ${data.b.cluster_name} vencem ${scoreA} métrica(s) cada.`
              : `🏆 ${scoreA > scoreB ? data.a.cluster_name : data.b.cluster_name} leva vantagem geral (${Math.max(scoreA, scoreB)} × ${Math.min(scoreA, scoreB)} métricas).`}
          </Banner>
          <Section title="Comparativo detalhado" sub="🏆 = melhor nessa métrica" />
          <table className="mdb">
            <thead><tr><th>Métrica</th><th>🔵 {data.a.cluster_name}</th><th>🟠 {data.b.cluster_name}</th><th>Quem está melhor</th></tr></thead>
            <tbody>
              {metrics.map(([m, va, vb, better, fmt], i) => {
                const w = winner(va, vb, better)
                return (
                  <tr key={i}>
                    <td style={{ color: 'var(--text-muted)' }}>{m}</td>
                    <td className="mono" style={{ color: w < 0 ? '#00ED64' : 'var(--text-pri)', fontWeight: w < 0 ? 700 : 400 }}>{w < 0 ? '🏆 ' : ''}{fmt(va)}</td>
                    <td className="mono" style={{ color: w > 0 ? '#00ED64' : 'var(--text-pri)', fontWeight: w > 0 ? 700 : 400 }}>{w > 0 ? '🏆 ' : ''}{fmt(vb)}</td>
                    <td style={{ fontSize: 12, color: 'var(--text-muted)' }}>
                      {better === null ? '—' : w === 0 ? 'empate' : `${w < 0 ? data.a.cluster_name : data.b.cluster_name} (${better === 'low' ? 'menor é melhor' : 'maior é melhor'})`}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </>
      )}
    </>
  )
}
