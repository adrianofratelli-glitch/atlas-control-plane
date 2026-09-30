import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
let source = (await readFile(new URL('../src/api.js', import.meta.url), 'utf8')).replaceAll('import.meta.env', '({})')
source = source.replace(/from (["'])axios\1/g, `from '${new URL('../node_modules/axios/index.js', import.meta.url).href}'`)
source = source.replace("'./assistant-stream.js'", JSON.stringify(new URL('../src/assistant-stream.js', import.meta.url).href))
const api = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))
const encoder = new TextEncoder()
test('leaving a chat iterator cancels and releases the HTTP stream', async () => {
 const original=globalThis.fetch;let cancelled=false
 const response=new Response(new ReadableStream({start(c){c.enqueue(encoder.encode('resposta'))},cancel(){cancelled=true}}));globalThis.fetch=async()=>response
 try { for await(const text of api.streamChat([])){assert.equal(text,'resposta');break}assert.equal(cancelled,true);assert.equal(response.body.locked,false) }finally{globalThis.fetch=original}
})
