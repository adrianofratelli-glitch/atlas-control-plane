import { H1 } from '@leafygreen-ui/typography'
import Badge from '@leafygreen-ui/badge'
import { Section, StatusDot } from '../components.jsx'

export default function Clusters({ clusters }) {
  return (
    <>
      <div className="page-head"><H1 style={{ color: 'var(--text-pri)' }}>Clusters da Organização</H1></div>
      <Section title="Frota" badge={`${clusters.length} cluster(s)`} />
      <table className="mdb">
        <thead>
          <tr><th>Projeto</th><th>Cluster</th><th>Tier</th><th>Região</th><th>Status</th><th>MongoDB</th><th>Tipo</th><th style={{ textAlign: 'right' }}>Custo/Mês (est.)</th></tr>
        </thead>
        <tbody>
          {clusters.map(c => (
            <tr key={`${c.project_id}:${c.cluster_name}`}>
              <td>{c.project_name}</td>
              <td className="mono" style={{ color: '#00ED64' }}>{c.cluster_name}</td>
              <td><Badge variant="blue">{c.tier}</Badge></td>
              <td style={{ color: 'var(--text-muted)' }}>{c.region_pretty}</td>
              <td><span className="row" style={{ gap: 6 }}><StatusDot status={c.status} /><span className="mono" style={{ fontSize: 12 }}>{c.status}</span></span></td>
              <td className="mono" style={{ color: 'var(--text-muted)' }}>{c.mongo_version}</td>
              <td className="mono" style={{ color: 'var(--text-muted)' }}>{c.cluster_type}</td>
              <td className="mono" style={{ textAlign: 'right' }}>${fmt(c.cost_usd)} <span style={{ color: '#5f869e' }}>/</span> <span style={{ color: '#00ED64' }}>R${fmt(c.cost_brl)}</span></td>
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 10 }}>
        Custos estimados pela tabela pública (AWS us-east-1, replica set 3 nós) — variam por provedor e região.
      </div>
    </>
  )
}
const fmt = (n) => Math.round(n).toLocaleString('pt-BR')
