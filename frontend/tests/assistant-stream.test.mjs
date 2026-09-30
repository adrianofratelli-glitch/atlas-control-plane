import test from 'node:test'
import assert from 'node:assert/strict'
import { readAssistantStream } from '../src/assistant-stream.js'
const encoder = new TextEncoder()
function body(chunks) { return new ReadableStream({start(c) { for (const chunk of chunks) c.enqueue(chunk); c.close() }}) }
test('parses split UTF-8, CRLF and final frame without newline', async () => {
  const bytes = encoder.encode('{"type":"text","text":"índice"}\r\n{"type":"done"}')
  const events = []
  for await (const event of readAssistantStream(body(Array.from(bytes, byte => Uint8Array.of(byte))))) events.push(event)
  assert.deepEqual(events, [{type:'text', text:'índice'}, {type:'done'}])
})
test('rejects EOF without done', async () => {
  await assert.rejects(async () => { for await (const _ of readAssistantStream(body([encoder.encode('{"type":"text","text":"parcial"}\n')]))) {} }, /interrompida/)
})
test('cancels and releases reader when caller leaves early', async () => {
  let cancelled = false
  const stream = new ReadableStream({start(c) { c.enqueue(encoder.encode('{"type":"text","text":"oi"}\n')) }, cancel() { cancelled = true }})
  for await (const _ of readAssistantStream(stream)) break
  assert.equal(cancelled, true); assert.equal(stream.locked, false)
})
test('rejects malformed JSON and releases reader', async () => {
  const stream = body([encoder.encode('{broken}\n')])
  await assert.rejects(async () => { for await (const _ of readAssistantStream(stream)) {} })
  assert.equal(stream.locked, false)
})
