import { useEffect, useState } from 'react'
import { H1 } from '@leafygreen-ui/typography'
import Badge from '@leafygreen-ui/badge'
import Card from '@leafygreen-ui/card'
import { Leaf, KpiGrid, Kpi, Section, StatusDot } from '../components.jsx'
import { getAlerts } from '../api.js'

const fmt = (n) => Math.round(n).toLocaleString('pt-BR')
const dotColor = (s) => s === 'IDLE' ? '#00ED64' : s === 'PAUSED' ? '#f97316' : '#06b6d4'
function mode(arr) { const m = {}; let best = arr[0], bc = 0; arr.forEach(v => { m[v] = (m[v] || 0) + 1; if (m[v] > bc) { bc = m[v]; best = v } }); return best }

export default function Overview({ clusters }) {
  const [alerts, setAlerts] = useState(0)
  useEffect(() => {
    const ids = [...new Set(clusters.map(c => c.project_id))]
    if (ids.length) getAlerts(ids).then(setAlerts).catch(() => {})
  }, [clusters])

  const projects = [...new Set(clusters.map(c => c.project_name))]
  const idle = clusters.filter(c => c.status === 'IDLE').length
  const costBrl = clusters.reduce((s, c) => s + c.cost_brl, 0)
  const costUsd = clusters.reduce((s, c) => s + c.cost_usd, 0)
  const dedic = clusters.filter(c => c.tier !== 'Free/Shared')
  const topTier = dedic.length ? mode(dedic.map(c => c.tier)) : '—'
  const versions = [...new Set(clusters.map(c => c.mongo_version))]
  const regions = [...new Set(clusters.map(c => c.region_pretty))]
  const outdated = clusters.filter(c => parseInt(c.mongo_version) < 7).length

  // Cost per project
  const byProject = projects.map(p => ({
    name: p,
    cost: clusters.filter(c => c.project_name === p).reduce((s, c) => s + c.cost_usd, 0),
    count: clusters.filter(c => c.project_name === p).length,
  })).sort((a, b) => b.cost - a.cost)
  const maxCost = Math.max(...byProject.map(p => p.cost), 1)

  return (
    <>
      <div className="page-head"><Leaf size={26} /><H1 style={{ color: 'var(--text-pri)' }}>Visão Geral</H1></div>

      <KpiGrid>
        <Kpi label="Total Clusters" value={clusters.length} delta={`${dedic.length} dedicados`} />
        <Kpi label="Projetos" value={projects.length} color="#06b6d4" />
        <Kpi label="Estáveis (IDLE)" value={`${idle}/${clusters.length}`}
             delta={idle === clusters.length ? 'todos estáveis' : `${clusters.length - idle} pausado(s)/em transição`}
             color={idle === clusters.length ? '#00ED64' : '#f97316'} />
        <Kpi label="Tier + comum" value={topTier} color="#00A35C" />
        <Kpi label="Custo Est./Mês" value={`R$ ${fmt(costBrl)}`} delta={`≈ USD ${fmt(costUsd)} · tabela us-east-1`} />
        <Kpi label="Alertas" value={alerts} delta={alerts === 0 ? '✓ nenhum' : `↑ ${alerts} abertos`}
             color={alerts === 0 ? '#00ED64' : '#f97316'} />
      </KpiGrid>

      {/* Row of informational chips */}
      <div className="row" style={{ marginBottom: 22, gap: 8 }}>
        <Badge variant={outdated ? 'yellow' : 'green'}>MongoDB: {versions.join(', ')}{outdated ? ` · ${outdated} desatualizado(s)` : ' · atualizado'}</Badge>
        <Badge variant="blue">{regions.length} região(ões): {regions.join(' · ')}</Badge>
        <Badge variant="lightgray">{[...new Set(clusters.map(c => c.cluster_type))].join(' · ')}</Badge>
      </div>

      <div className="two-col" style={{ display: 'grid', gridTemplateColumns: '3fr 2fr', gap: 24 }}>
        {/* Fleet */}
        <div>
          <Section title="Frota de Clusters" badge={String(clusters.length)} />
          {clusters.map(c => (
            <div key={`${c.project_id}:${c.cluster_name}`} className="fleet-card" style={{ borderLeft: `3px solid ${dotColor(c.status)}` }}>
              <StatusDot status={c.status} />
              <div style={{ flex: 1 }}>
                <div className="mono" style={{ fontWeight: 700, color: 'var(--text-pri)' }}>{c.cluster_name}</div>
                <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 2 }}>{c.tier} · {c.region_pretty} · MongoDB {c.mongo_version}</div>
              </div>
              <div style={{ textAlign: 'right' }}>
                <div className="mono" style={{ fontSize: 11, fontWeight: 700, color: dotColor(c.status) }}>{c.status}</div>
                <div className="mono" style={{ fontSize: 11, color: '#00A35C' }}>R${fmt(c.cost_brl)}/mês</div>
              </div>
            </div>
          ))}
        </div>

        {/* Cost per project */}
        <div>
          <Section title="Custo por Projeto" badge="USD/mês" />
          <Card className="panel" darkMode>
            {byProject.map((p, i) => (
              <div key={i} style={{ marginBottom: 14 }}>
                <div className="row" style={{ justifyContent: 'space-between', marginBottom: 5 }}>
                  <span style={{ fontSize: 13, color: 'var(--text-pri)' }}>{p.name} <span style={{ color: 'var(--text-muted)', fontSize: 11 }}>· {p.count} cluster(s)</span></span>
                  <span className="mono" style={{ fontSize: 13, color: '#00ED64' }}>${fmt(p.cost)}</span>
                </div>
                <div style={{ height: 8, background: 'var(--bg-secondary)', borderRadius: 4, overflow: 'hidden' }}>
                  <div style={{ width: `${(p.cost / maxCost) * 100}%`, height: '100%', background: 'linear-gradient(90deg,#00A35C,#00ED64)' }} />
                </div>
              </div>
            ))}
          </Card>
        </div>
      </div>

      <div style={{ fontSize: 12, color: 'var(--text-muted)', marginTop: 20 }}>
        💡 Métricas vivas (CPU, conexões, IOPS) ficam sob demanda nas abas <b>Scale</b>, <b>Health Score</b> e <b>FinOps</b> — para manter a Visão Geral rápida.
        Custos são <b>estimativas de tabela</b> (AWS us-east-1) — a fatura real está na aba <b>FinOps</b>.
      </div>
    </>
  )
}
