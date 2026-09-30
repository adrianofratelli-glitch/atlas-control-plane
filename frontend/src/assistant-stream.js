// NDJSON parser: reject incomplete streams and always release/cancel the reader.
export async function* readAssistantStream(body) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = '', completed = false
  try {
    while (true) {
      const { value, done } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      if (buffer.length > 1024 * 1024) throw new Error('Resposta excedeu o tamanho permitido.')
      const lines = buffer.split('\n')
      buffer = lines.pop()
      if (done && buffer.trim()) { lines.push(buffer); buffer = '' }
      for (const line of lines) {
        if (!line.trim()) continue
        const event = JSON.parse(line)
        if (completed) throw new Error('Evento recebido após a conclusão.')
        if (event.type === 'done') completed = true
        yield event
      }
      if (done) break
    }
    if (!completed) throw new Error('Conexão interrompida antes da conclusão. Atualize as ações para conferir operações pendentes.')
  } finally {
    reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
