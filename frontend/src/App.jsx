import { lazy, Suspense, useEffect, useState } from 'react'
import { H3, Body } from '@leafygreen-ui/typography'
import Banner from '@leafygreen-ui/banner'
import Icon from '@leafygreen-ui/icon'
import { Leaf } from './components.jsx'
import { getConfig, getClusters } from './api.js'
import { ClusterContext } from './cluster-context.jsx'

const Overview = lazy(() => import('./pages/Overview.jsx'))
const Scale = lazy(() => import('./pages/Scale.jsx'))
const PerformanceAdvisor = lazy(() => import('./pages/PerformanceAdvisor.jsx'))
const Profiler = lazy(() => import('./pages/Profiler.jsx'))
const FinOps = lazy(() => import('./pages/FinOps.jsx'))
const Health = lazy(() => import('./pages/Health.jsx'))
const Chat = lazy(() => import('./pages/Chat.jsx'))

const NAV = [
  { id: 'overview', label: 'Visão Geral', icon: 'Dashboard', Comp: Overview },
  { id: 'indexes', label: 'Índices', icon: 'LightningBolt', Comp: PerformanceAdvisor },
  { id: 'profiler', label: 'Consultas', icon: 'MagnifyingGlass', Comp: Profiler },
  { id: 'health', label: 'Saúde', icon: 'Gauge', Comp: Health },
  { id: 'scale', label: 'Escala', icon: 'Charts', Comp: Scale },
  { id: 'finops', label: 'FinOps', icon: 'Coin', Comp: FinOps },
  { id: 'chat', label: 'Assistente', icon: 'Sparkle', Comp: Chat },
]

export default function App() {
  const [active, setActive] = useState('overview')
  const [config, setConfig] = useState(null)
  const [clusters, setClusters] = useState([])
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([getConfig(), getClusters()])
      .then(([cfg, cls]) => { setConfig(cfg); setClusters(cls) })
      .catch(e => setError(e?.response?.data?.detail || e.message))
      .finally(() => setLoading(false))
  }, [])

  const Current = NAV.find(n => n.id === active)?.Comp || Overview

  return (
    <div className="app-shell" data-pov-shell>
      <a className="pov-skip-link" href="#conteudo-principal">Pular para o conteúdo</a>
      <nav className="sidenav">
        <div className="sidenav-brand">
          <Leaf size={28} />
          <div>
            <div style={{ fontWeight: 800, fontSize: 15, color: 'var(--text-pri)' }}>Torre</div>
            <div className="mono" style={{ fontSize: 9, color: 'var(--text-muted)', letterSpacing: 1.5 }}>ATLAS CONTROL PLANE</div>
          </div>
        </div>
        {NAV.map((n, i) => n.section
          ? <div key={i} className="sidenav-section">{n.section}</div>
          : <button key={n.id} className={`sidenav-item ${active === n.id ? 'active' : ''}`} aria-current={active === n.id ? 'page' : undefined} onClick={() => setActive(n.id)}>
              <Icon glyph={n.icon} size="large" role="presentation" />{n.label}
            </button>
        )}
        <div className="spacer" />
        {config && (
          <div className="mono" style={{ fontSize: 10, color: 'var(--text-muted)', padding: '8px 12px', borderTop: '1px solid var(--border-subtle)' }}>
            {config.atlas ? '🟢' : '🔴'} Atlas&nbsp;&nbsp;{config.anthropic ? '🟢' : '⚪'} Claude&nbsp;&nbsp;{config.mongodb ? '🟢' : '⚪'} Mongo
          </div>
        )}
      </nav>

      <main id="conteudo-principal" tabIndex={-1} className="main">
        {loading && <Body style={{ color: 'var(--text-muted)' }}>Conectando ao MongoDB Atlas…</Body>}
        {error && <Banner variant="danger">Erro ao carregar: {error}</Banner>}
        {!loading && !error && config && (
          <ClusterContext clusters={clusters}>
          <div className="page-enter" key={active}>
            <Suspense fallback={<Body style={{ color: 'var(--text-muted)' }}>Carregando módulo…</Body>}>
              {active !== 'chat' && <Current clusters={clusters} config={config} />}
            </Suspense>
          </div>
          <div hidden={active !== 'chat'}>
            <Suspense fallback={<Body>Carregando Assistente…</Body>}>
              <Chat clusters={clusters} config={config} active={active === 'chat'} />
            </Suspense>
          </div>
          </ClusterContext>
        )}
      </main>
    </div>
  )
}
