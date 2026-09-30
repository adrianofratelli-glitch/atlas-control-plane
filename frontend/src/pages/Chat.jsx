import { useState, useRef, useEffect, memo } from 'react'
import Button from '@leafygreen-ui/button'
import Banner from '@leafygreen-ui/banner'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Leaf } from '../components.jsx'
import { streamAssistant, getAssistantCapabilities, getAssistantActions, decideAssistantAction, listConversations, getConversation, deleteConversation } from '../api.js'
import { clusterKey } from './_picker.jsx'
import { useClusterSelection } from '../cluster-context.jsx'

const STARTERS = [
  ['Melhorar desempenho', 'Liste todos os índices recomendados para este cluster e explique quais consultas cada um pode melhorar.'],
  ['Conhecer meus dados', 'Mostre os bancos e coleções deste cluster e me ajude a entender quais dados tenho.'],
  ['Investigar lentidão', 'Verifique as consultas lentas e as métricas do cluster e explique o que merece atenção.'],
  ['Fazer uma alteração', 'Quero atualizar alguns documentos. Me ajude a escolher a coleção e definir quais registros e campos alterar.'],
]
const STATE = { pending: 'Aguardando aprovação', executing: 'Execução iniciada — confira o resultado', completed: 'Resultado recebido', cancelled: 'Cancelada', expired: 'Expirada', unknown: 'Resultado não confirmado' }

function sessionId() {
  try {
    const previous = sessionStorage.getItem('torre.assistantSession')
    if (previous) return previous
    const value = crypto.randomUUID().replaceAll('-', '')
    sessionStorage.setItem('torre.assistantSession', value)
    return value
  } catch { return crypto.randomUUID().replaceAll('-', '') }
}

const Bubble = memo(function Bubble({ message }) {
  return <article className={'bubble ' + message.role}>
    <div className="bubble-avatar">{message.role === 'user' ? 'Você' : <Leaf size={20} />}</div>
    <div className="bubble-body">
      <div className="bubble-name">{message.role === 'user' ? 'Você' : 'Assistente Torre'}</div>
      <div className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content || 'Consultando…'}</ReactMarkdown></div>
    </div>
  </article>
})

function ActionCard({ action, onDecide, disabled }) {
  const a = action.arguments, d = action.details, r = action.result
  const pending = action.state === 'pending' && action.expires_at * 1000 > Date.now()
  const changes = a.update ? Object.entries(a.update).flatMap(([op, fields]) => Object.entries(fields).map(([field, value]) => field + ': ' + (op === '$unset' ? 'remover campo' : JSON.stringify(value)) + (op === '$inc' ? ' (incremento)' : ''))) : []
  return <article className={'assistant-action ' + (action.destructive ? 'destructive' : '')}>
    <div className="assistant-action-heading"><strong>{d.operation}</strong><span>{STATE[action.state] || action.state}</span></div>
    <p><b>{d.cluster}</b>{d.namespace ? ' · ' + d.namespace : ''}</p>
    {d.documents != null && <p>{d.documents} documento(s) na prévia.</p>}
    {d.estimated_documents != null && <p>A coleção inteira será excluída. Estimativa: {d.estimated_documents} documento(s).</p>}
    {a.name && <p>Índice: <b>{a.name}</b></p>}
    {a.keys && <p>Campos do índice: {a.keys.map(k => Object.entries(k).map(([field, direction]) => field + (direction === 1 ? ' (crescente)' : direction === -1 ? ' (decrescente)' : ' (' + direction + ')')).join(', ')).join(' → ')}</p>}
    {changes.length > 0 && <ul>{changes.map((change, i) => <li key={i}>{change}</li>)}</ul>}
    {d.target_tier && <p>Nova capacidade: <b>{d.target_tier}</b></p>}
    {d.estimate && <p>Estimativa mensal de tabela: USD {d.estimate.monthly_usd ?? d.estimate.usd_month ?? d.estimate.usd ?? '—'} · consulte os detalhes.</p>}
    <p className="assistant-muted">{d.impact}</p>
    <details><summary>Conferir detalhes da operação</summary><pre>{JSON.stringify({ ...a, ...(d.estimate ? { estimativa: d.estimate } : {}) }, null, 2)}</pre></details>
    {r && <div className="assistant-result">
      {r.message || r.note || (r.status === 'submitted' ? 'Solicitação aceita; a conclusão depende do processamento no cluster.' : 'Operação confirmada pelo servidor.')}
      {r.inserted_count != null && <p>{r.inserted_count} documento(s) inserido(s).</p>}
      {r.modified_count != null && <p>{r.modified_count} documento(s) alterado(s), {r.matched_count} encontrado(s).</p>}
      {r.deleted_count != null && <p>{r.deleted_count} documento(s) excluído(s).</p>}
      {r.index && <p>Índice: {r.index}</p>}
      <details><summary>Resultado completo</summary><pre>{JSON.stringify(r, null, 2)}</pre></details>
    </div>}
    {pending && <div className="row assistant-action-buttons">
      <Button variant={action.destructive ? 'danger' : 'primary'} disabled={disabled} onClick={() => onDecide(action.id, 'approve')}>{action.destructive ? 'Aprovar exclusão' : 'Aprovar e executar'}</Button>
      <Button disabled={disabled} onClick={() => onDecide(action.id, 'cancel')}>Cancelar</Button>
      <small>Válida por 15 minutos. Não será executada ao enviar uma mensagem.</small>
    </div>}
  </article>
}

export default function Chat({ clusters, config, active = true }) {
  const [ctx, setCtx] = useClusterSelection(true)
  const key = clusterKey(ctx)
  const [session] = useState(sessionId)
  const [msgs, setMsgs] = useState([]), [input, setInput] = useState('')
  const [busy, setBusy] = useState(false), [decisionBusy, setDecisionBusy] = useState(false)
  const [error, setError] = useState(''), [connected, setConnected] = useState(false)
  const [trace, setTrace] = useState([]), [actions, setActions] = useState([])
  const [traceUrl, setTraceUrl] = useState('')
  const [caps, setCaps] = useState(null), [history, setHistory] = useState([]), [convId, setConvId] = useState(null)
  const requestRef = useRef(null), generation = useRef(0), lock = useRef(false), decisionLock = useRef(false), end = useRef(null)
  const context = { session_id: session, project_id: ctx?.project_id || '', cluster_name: ctx?.cluster_name || '' }

  useEffect(() => { getAssistantCapabilities().then(setCaps).catch(() => setError('Não foi possível carregar as ferramentas do Assistente.')) }, [])
  useEffect(() => { if (active) end.current?.scrollIntoView({ behavior: 'smooth', block: 'end' }) }, [msgs, trace, active])
  useEffect(() => {
    generation.current += 1
    requestRef.current?.abort()
    lock.current = false
    setBusy(false); setMsgs([]); setTrace([]); setActions([]); setConvId(null); setConnected(false); setError(''); setTraceUrl('')
    const run = generation.current
    getAssistantActions(context).then(a => { if (run === generation.current) setActions(a) }).catch(() => {})
    if (config.mongodb) listConversations().then(h => { if (run === generation.current) setHistory(h) }).catch(() => {})
    return () => { generation.current += 1; requestRef.current?.abort() }
  }, [key, session])
  useEffect(() => { if (!active) requestRef.current?.abort() }, [active])

  const refreshActions = async () => {
    const run = generation.current
    try { const a = await getAssistantActions(context); if (run === generation.current) setActions(a) }
    catch { if (run === generation.current) setError('Não foi possível consultar as ações. Tente atualizar novamente; não reenvie uma alteração sem conferir o resultado.') }
  }

  const send = async (text) => {
    if (!text.trim() || lock.current || decisionLock.current) return
    lock.current = true
    const run = generation.current, controller = new AbortController()
    requestRef.current = controller
    const timer = setTimeout(() => controller.abort(), 250000)
    const next = [...msgs.filter(m => m.content), { role: 'user', content: text.trim() }]
    setMsgs([...next, { role: 'assistant', content: '' }]); setInput(''); setBusy(true); setError(''); setTrace([]); setTraceUrl('')
    let response = ''
    try {
      for await (const event of streamAssistant({ ...context, messages: next.slice(-16).map(m => ({ role: m.role, content: m.content.slice(0, 12000) })), conversation_id: convId }, controller.signal)) {
        if (run !== generation.current) break
        if (event.type === 'connected') setConnected(true)
        if (event.type === 'trace_url') setTraceUrl(event.url)
        if (event.type === 'conversation') setConvId(event.id)
        if (event.type === 'text') { response += event.text; setMsgs([...next, { role: 'assistant', content: response }]) }
        if (event.type === 'tool_start') setTrace(t => [...t, { ...event, running: true }])
        if (event.type === 'tool_end') setTrace(t => t.map(item => item.id === event.id ? { ...event, running: false } : item))
        if (event.type === 'action') setActions(a => [event.action, ...a.filter(item => item.id !== event.action.id)])
        if (event.type === 'error') setError(event.message)
      }
      if (config.mongodb) listConversations().then(h => { if (run === generation.current) setHistory(h) }).catch(() => {})
    } catch (e) {
      if (run === generation.current) setError(e.name === 'AbortError' ? 'Conversa interrompida. As ações já preparadas continuam disponíveis; consulte seus estados antes de repetir um pedido.' : e.message)
    } finally {
      clearTimeout(timer)
      if (run === generation.current) {
        lock.current = false; setBusy(false); requestRef.current = null
        if (!response) setMsgs(next)
        setTrace(t => t.map(item => item.running ? { ...item, running: false, ok: false, message: 'Consulta interrompida; resultado indisponível' } : item))
      }
    }
  }

  const decide = async (id, decision) => {
    if (decisionLock.current || lock.current) return
    decisionLock.current = true; setDecisionBusy(true); setError('')
    const run = generation.current
    try {
      const action = await decideAssistantAction(id, context, decision)
      if (run === generation.current) setActions(a => a.map(item => item.id === id ? action : item))
    } catch {
      if (run === generation.current) {
        setError('Resposta da ação indisponível. Atualize as ações para consultar o estado; a execução não será repetida automaticamente.')
        setActions(a => a.map(item => item.id === id ? { ...item, state: 'unknown' } : item))
      }
    } finally { decisionLock.current = false; setDecisionBusy(false) }
  }

  const openConversation = async (id) => {
    if (lock.current || decisionLock.current) return
    if (!id) { setMsgs([]); setConvId(null); setTrace([]); setError(''); return }
    const run = generation.current
    lock.current = true; setBusy(true)
    try {
      const messages = await getConversation(id)
      if (run === generation.current) { setMsgs(messages.map(m => ({ role: m.role, content: m.content }))); setConvId(id); setTrace([]) }
    } catch { if (run === generation.current) setError('Não foi possível abrir o histórico.') }
    finally { if (run === generation.current) { lock.current = false; setBusy(false) } }
  }

  const removeConversation = async () => {
    if (!convId || lock.current || decisionLock.current) return
    lock.current = true; setBusy(true)
    const run = generation.current
    try {
      await deleteConversation(convId)
      if (run !== generation.current) return
      setHistory(h => h.filter(item => item.id !== convId))
      setMsgs([]); setConvId(null); setTrace([])
    } catch { if (run === generation.current) setError('Não foi possível apagar a conversa.') }
    finally { if (run === generation.current) { lock.current = false; setBusy(false) } }
  }

  if (!config.anthropic) return <Banner variant="warning">Configure a chave do modelo no servidor para habilitar o Assistente.</Banner>
  const matchingHistory = history.filter(h => (h.cluster || '') === context.cluster_name && h.project_id === context.project_id)
  return <div className="assistant-workspace">
    <header className="assistant-header">
      <div><div className="row"><Leaf size={28} /><h1>Converse com seus dados</h1></div><p>Descreva o que você precisa. A Torre consulta, explica e prepara a operação.</p></div>
      <label className="assistant-cluster">Cluster da conversa
        <select value={key} disabled={busy || decisionBusy} onChange={e => setCtx(clusters.find(c => clusterKey(c) === e.target.value) || null)}>
          <option value="">MongoDB geral · sem conexão com dados</option>
          {clusters.map(c => <option key={clusterKey(c)} value={clusterKey(c)}>{c.project_name} / {c.cluster_name}</option>)}
        </select>
      </label>
    </header>
    <div className="assistant-status"><span className={connected ? 'live' : ''}>{connected ? 'MCP conectado nesta conversa' : 'MCP pronto para conectar'}</span><span>{caps ? caps.tools.length + ' ferramentas' : 'Carregando ferramentas…'}</span><span>Alterações com aprovação</span>
      {traceUrl && <a href={traceUrl} target="_blank" rel="noreferrer" className="langfuse-link" title="Abrir trace completo (tokens, custo, latência por passo) no Langfuse">🔭 Ver trace no Langfuse ↗</a>}
    </div>
    {!ctx && <Banner variant="info">Selecione um cluster acima para consultar índices, acessar documentos e preparar operações.</Banner>}
    {ctx && !config.mongodb && <Banner variant="info">Métricas e recomendações Atlas disponíveis conforme as permissões. Para documentos e coleções, configure a conexão de dados no servidor.</Banner>}
    <div className="assistant-history row">
      <Button size="small" disabled={busy || decisionBusy} onClick={() => openConversation('')}>Nova conversa</Button>
      {matchingHistory.length > 0 && <select aria-label="Histórico deste cluster" value={convId || ''} disabled={busy || decisionBusy} onChange={e => openConversation(e.target.value)}><option value="">Histórico deste cluster</option>{matchingHistory.map(h => <option key={h.id} value={h.id}>{h.title}</option>)}</select>}
      {convId && <Button size="small" variant="dangerOutline" disabled={busy || decisionBusy} onClick={removeConversation}>Apagar conversa</Button>}
    </div>
    {msgs.length === 0 && <section className="assistant-starters" aria-label="Sugestões para começar">{STARTERS.map(([title, prompt]) => <button key={title} disabled={busy || decisionBusy} onClick={() => send(prompt)}><strong>{title}</strong><span>{prompt}</span><span aria-hidden="true">↗</span></button>)}</section>}
    <section aria-label="Conversa">{msgs.map((m, i) => <Bubble key={i} message={m} />)}</section>
    {trace.length > 0 && <section className="assistant-trace" aria-label="Atividade das ferramentas"><h2>O que a Torre consultou</h2>{trace.map(t => <div key={t.id}><span aria-hidden="true">{t.running ? '◌' : t.ok ? '✓' : '!'}</span><span>{t.label}</span><small>{t.running ? 'Consultando…' : t.message}</small></div>)}</section>}
    {error && <div role="alert" className="assistant-error">{error}</div>}
    <section className="assistant-actions" aria-label="Operações e aprovações"><div className="row"><h2>Operações desta sessão</h2><Button size="xsmall" onClick={refreshActions} disabled={decisionBusy}>Atualizar ações</Button></div>{actions.length === 0 ? <p className="assistant-muted">Quando você pedir uma alteração, a prévia e o botão de aprovação aparecerão aqui.</p> : actions.map(a => <ActionCard key={a.id} action={a} onDecide={decide} disabled={busy || decisionBusy} />)}</section>
    <div ref={end} />
    <form className="assistant-composer" onSubmit={e => { e.preventDefault(); send(input) }}>
      <label htmlFor="assistant-input">O que você quer fazer no cluster?</label>
      <textarea id="assistant-input" value={input} maxLength={12000} onChange={e => setInput(e.target.value)} placeholder="Ex.: encontre os pedidos pendentes de hoje e me mostre um resumo por cliente" rows={3} disabled={decisionBusy} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input) } }} />
      <div className="row"><small>Enter envia · Shift + Enter quebra a linha</small>{busy ? <Button onClick={() => requestRef.current?.abort()}>Interromper consulta</Button> : <Button type="submit" variant="primary" disabled={!input.trim() || decisionBusy}>Enviar pedido</Button>}</div>
    </form>
  </div>
}
