// components.jsx — reusable MongoDB Atlas-style building blocks
import { Body } from '@leafygreen-ui/typography'

export function Leaf({ size = 28, color = '#00ED64' }) {
  return (
    <svg width={size} height={size} viewBox="0 0 28 28" fill="none">
      <path d="M14 2C8.48 2 4 6.7 4 12.5c0 4.1 2.1 7.7 5.3 9.7l.7 3.3c.1.3.3.5.6.5h6.8c.3 0 .5-.2.6-.5l.7-3.3C21.9 20.2 24 16.6 24 12.5 24 6.7 19.52 2 14 2zm.8 16.2v3.3c0 .1-.1.2-.2.2h-1.2c-.1 0-.2-.1-.2-.2v-3.3C11.1 17.4 9.5 15 9.5 12.5c0-2.5 2-4.5 4.5-4.5s4.5 2 4.5 4.5c0 2.5-1.6 4.9-3.7 5.7z" fill={color} />
    </svg>
  )
}

export function KpiGrid({ children }) {
  return <div className="kpi-grid">{children}</div>
}

export function Kpi({ label, value, delta, color = '#00ED64' }) {
  return (
    <div className="kpi" style={{ borderTopColor: color }}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value mono" style={{ color }}>{value}</div>
      {delta && <div className="kpi-delta">{delta}</div>}
    </div>
  )
}

export function Section({ title, badge, sub }) {
  return (
    <div className="section-title" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <span>{title}</span>
      {badge && <span style={{ fontSize: 10, padding: '2px 8px', borderRadius: 3, background: 'rgba(0,237,100,0.1)', color: '#00ED64', border: '1px solid rgba(0,237,100,0.25)' }}>{badge}</span>}
      {sub && <span style={{ marginLeft: 'auto', fontSize: 11, color: '#5f869e', textTransform: 'none', letterSpacing: 0 }}>{sub}</span>}
    </div>
  )
}

export function StatusDot({ status }) {
  const c = status === 'IDLE' ? '#00ED64' : status === 'PAUSED' ? '#f97316' : '#06b6d4'
  return <span className="dot" style={{ background: c, boxShadow: `0 0 8px ${c}` }} />
}

export function Empty({ icon, title, hint }) {
  return (
    <div className="empty">
      <div style={{ fontSize: 34, marginBottom: 8 }}>{icon}</div>
      <div style={{ fontSize: 15, fontWeight: 700, color: 'var(--text-pri)', marginBottom: 6 }}>{title}</div>
      <Body style={{ color: 'var(--text-muted)' }}>{hint}</Body>
    </div>
  )
}

// Mini SVG chart (CPU area + queries line) — lightweight, compact, fixed height
export function MiniChart({ series, height = 160 }) {
  if (!series || series.error || !series.timestamps || series.timestamps.length === 0)
    return <div className="empty" style={{ padding: 22 }}>Sem dados históricos disponíveis.</div>
  const w = 1000, h = 300, pad = 6   // internal viewBox; render uses fixed height
  const cpu = series.cpu || []
  const q = series.ops_query || []
  const n = cpu.length
  const maxCpu = Math.max(10, ...cpu)
  const maxQ = Math.max(1, ...q)
  const x = (i) => pad + (i / Math.max(1, n - 1)) * (w - 2 * pad)
  const yC = (v) => h - pad - (v / maxCpu) * (h - 2 * pad)
  const yQ = (v) => h - pad - (v / maxQ) * (h - 2 * pad)
  const lineCpu = cpu.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i)},${yC(v)}`).join(' ')
  const areaCpu = `${lineCpu} L${x(n - 1)},${h - pad} L${x(0)},${h - pad} Z`
  const lineQ = q.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i)},${yQ(v)}`).join(' ')
  return (
    <div style={{ background: 'var(--bg-card)', border: '1px solid var(--border-subtle)', borderRadius: 8, padding: '10px 12px' }}>
      <div className="row" style={{ gap: 18, marginBottom: 6, fontSize: 11 }}>
        <span className="mono" style={{ color: '#00ED64' }}>● CPU % (máx {maxCpu.toFixed(0)})</span>
        <span className="mono" style={{ color: '#06b6d4' }}>● Queries/s (máx {maxQ.toFixed(0)})</span>
        <span className="mono" style={{ color: '#5f869e', marginLeft: 'auto' }}>últimas 24h</span>
      </div>
      <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ width: '100%', height: `${height}px`, display: 'block' }}>
        {[0.33, 0.66, 1].map((g, i) => (
          <line key={i} x1={pad} x2={w - pad} y1={g * (h - pad)} y2={g * (h - pad)} stroke="rgba(255,255,255,0.05)" />
        ))}
        <path d={areaCpu} fill="rgba(0,237,100,0.10)" />
        <path d={lineCpu} fill="none" stroke="#00ED64" strokeWidth="3" vectorEffect="non-scaling-stroke" />
        <path d={lineQ} fill="none" stroke="#06b6d4" strokeWidth="3" vectorEffect="non-scaling-stroke" />
      </svg>
    </div>
  )
}
