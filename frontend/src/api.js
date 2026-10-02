import { readAssistantStream } from './assistant-stream.js'
// api.js — axios client for Torre's FastAPI backend
import axios from 'axios'

// Optional: set VITE_API_TOKEN to match the backend's API_AUTH_TOKEN when the
// server is reachable beyond localhost.
const API_TOKEN = import.meta.env.VITE_API_TOKEN || ''
const authHeaders = API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}

const http = axios.create({ baseURL: '/api', timeout: 60000, headers: authHeaders })

export const getConfig      = () => http.get('/config').then(r => r.data)
export const getClusters    = () => http.get('/clusters').then(r => r.data.clusters)
export const getAlerts      = (ids) => http.get('/alerts', { params: { project_ids: ids.join(',') } }).then(r => r.data.open_alerts)
export const getInvoice     = () => http.get('/invoice').then(r => r.data.amount_usd)

const c = (pid, name) => `/cluster/${pid}/${encodeURIComponent(name)}`
export const getPA          = (pid, name) => http.get(`${c(pid, name)}/pa`).then(r => r.data)
export const getSlow        = (pid, name) => http.get(`${c(pid, name)}/slow`).then(r => r.data)
export const getMeasurements= (pid, name) => http.get(`${c(pid, name)}/measurements`).then(r => r.data)
export const getSeries      = (pid, name) => http.get(`${c(pid, name)}/series`).then(r => r.data)
export const getHealth      = (pid, name, status, mv) => http.get(`${c(pid, name)}/health`, { params: { status, mongo_version: mv } }).then(r => r.data)
export const getLiveMetrics = (pid, name, tier) => http.get(`${c(pid, name)}/live`, { params: { tier } }).then(r => r.data)
export const getScaling     = (pid, name, tier) => http.get(`${c(pid, name)}/scaling`, { params: { tier } }).then(r => r.data)
export const scaleCluster   = (pid, name, newTier) => http.post(`${c(pid, name)}/scale`, { new_tier: newTier }).then(r => r.data)
export const createIndex    = (namespace, indexKeys, project_id, cluster_name) => http.post('/index', { namespace, index_keys: indexKeys, project_id, cluster_name }).then(r => r.data)
export const getFinops      = () => http.get('/finops').then(r => r.data)
export const explainQuery   = (namespace, filter, project_id, cluster_name) => http.post('/explain', { namespace, filter, project_id, cluster_name }).then(r => r.data)

// Conversation history (persisted in Atlas — requires MONGODB_URI on the backend)
export const listConversations  = (q = '') => http.get('/chat/conversations', { params: { q } }).then(r => r.data.conversations)
export const getConversation    = (id) => http.get(`/chat/conversations/${id}`).then(r => r.data.messages)
export const deleteConversation = (id) => http.delete(`/chat/conversations/${id}`).then(r => r.data)

// Streaming helpers (fetch to read text chunks)
async function* streamPost(url, payload, onResponse) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders },
    body: JSON.stringify(payload),
  })
  if (!res.ok || !res.body) {
    let detail = `Erro HTTP ${res.status}`
    try { detail = (await res.json()).detail || detail } catch { /* non-JSON body */ }
    throw new Error(detail)
  }
  onResponse?.(res)
  const reader = res.body.getReader()
  try {
    const dec = new TextDecoder()
    while (true) {
      const { done, value } = await reader.read()
      if (done) {
        const tail = dec.decode()
        if (tail) yield tail
        break
      }
      yield dec.decode(value, { stream: true })
    }
  } finally {
    reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}

export const streamChat = (messages, project_id, cluster_name, conversation_id, onResponse) =>
  streamPost('/api/chat', { messages, project_id, cluster_name, conversation_id }, onResponse)

export async function* streamAnalyze(project_id, cluster_name) {
  for await (const event of streamAssistant({ session_id: crypto.randomUUID(), project_id, cluster_name, mode: 'report', messages: [{ role: 'user', content: 'Relatório técnico de performance com evidências MCP. Destaque Advisor, consultas recentes, capacidade e limitações; não prepare ações.' }] })) {
    if (event.type === 'text') yield event.text
    if (event.type === 'error') throw new Error(event.message)
  }
}

// Downloads the analysis report as PDF (or Markdown, if fpdf2 is unavailable)
export async function downloadReport(cluster_name, analysis, health_score = null, health_issues = null) {
  const res = await fetch('/api/report', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders },
    body: JSON.stringify({ cluster_name, analysis, health_score, health_issues }),
  })
  if (!res.ok) throw new Error(`Erro ao gerar relatório (HTTP ${res.status})`)
  const blob = await res.blob()
  const ext = (res.headers.get('Content-Type') || '').includes('pdf') ? 'pdf' : 'md'
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `torre-${cluster_name}.${ext}`
  a.click()
  URL.revokeObjectURL(a.href)
}


export const getAssistantCapabilities = () => http.get('/assistant/capabilities').then(r => r.data)
export const getAssistantActions = (context) => http.post('/assistant/actions/list', context).then(r => r.data.actions)
export const decideAssistantAction = (id, context, decision) => http.post('/assistant/actions/' + encodeURIComponent(id), { ...context, decision }).then(r => r.data)
export async function* streamAssistant(payload, signal) {
  const res = await fetch('/api/assistant', {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders },
    body: JSON.stringify(payload), signal,
  })
  if (!res.ok || !res.body) {
    let detail = 'Erro HTTP ' + res.status
    try { detail = (await res.json()).detail || detail } catch { /* invalid JSON */ }
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  for await (const event of readAssistantStream(res.body)) {
    yield event
  }
}
