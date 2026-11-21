import test from 'node:test'
import assert from 'node:assert/strict'
import { createSSEParser, consumeSSE, createFusionClient } from './fusion-api.js'

test('SSE跨chunk、CRLF、注释及多行data正确解析', () => {
  const events = []
  const parser = createSSEParser(event => events.push(event))
  parser.push(': heartbeat\r\nid: e1\r\ndata: {"type":"state_snapshot",\r\n')
  parser.push('data: "payload":{"article_content":"中文"}}\r\n\r')
  parser.push('\ndata: {"type":"run_completed","payload":{}}\n\n')
  parser.finish()
  assert.equal(events.length, 2)
  assert.equal(events[0].event_id, 'e1')
  assert.equal(events[0].payload.article_content, '中文')
})

test('UTF-8多字节跨网络chunk不损坏正文', async () => {
  const bytes = new TextEncoder().encode('data: {"type":"answer_delta","payload":{"content":"中文答复"}}\n\n')
  const response = new Response(new ReadableStream({ start(controller) { for (let i = 0; i < bytes.length; i += 2) controller.enqueue(bytes.slice(i, i + 2)); controller.close() } }), { headers: { 'content-type': 'text/event-stream' } })
  const events = []
  await consumeSSE(response, event => events.push(event))
  assert.equal(events[0].payload.content, '中文答复')
})

test('格式损坏的SSE明确失败，不把断线误报成功', () => {
  const parser = createSSEParser(() => {})
  assert.throws(() => parser.push('data: {bad json}\n\n'), /流事件格式/)
})

test('请求包含Bearer与固定幂等键，不将Token放入URL', async () => {
  const requests = []
  const client = createFusionClient({ tokenProvider: () => 'test-token', fetchImpl: async (url, options) => { requests.push({ url, options }); return Response.json({ insight_id: 'i1' }) } })
  await client.createInsight({ session_id: 's1', turn_id: 't1', summary: '问题', confirmed: true }, 'op-1')
  assert.equal(requests[0].url, '/api/v1/insights')
  assert.equal(requests[0].options.headers.Authorization, 'Bearer test-token')
  assert.equal(requests[0].options.headers['Idempotency-Key'], 'op-1')
  assert.equal(JSON.parse(requests[0].options.body).confirmed, true)
})

test('409状态冲突保留错误信息，调用方可刷新而非盲目重试', async () => {
  const client = createFusionClient({ tokenProvider: () => '', fetchImpl: async () => Response.json({ detail: '任务版本已改变' }, { status: 409 }) })
  await assert.rejects(client.actOnTask('a', { action: 'approve_text', expected_state_version: 2 }, 'op-2'), error => error.status === 409 && /任务版本已改变/.test(error.message))
})
