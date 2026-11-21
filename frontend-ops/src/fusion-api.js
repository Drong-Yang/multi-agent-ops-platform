export function createSSEParser(onEvent) {
  let buffer = ''
  function emit(block) {
    const lines = block.split('\n'), data = [], ids = []
    for (const line of lines) {
      if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
      else if (line.startsWith('id:')) ids.push(line.slice(3).trim())
    }
    if (!data.length || data.join('\n') === '[DONE]') return
    let event
    try { event = JSON.parse(data.join('\n')) } catch { throw new Error('流事件格式损坏，请刷新已保存状态') }
    if (!event.event_id && ids.length) event.event_id = ids.at(-1)
    onEvent(event)
  }
  return {
    push(text) {
      buffer = (buffer + text).replace(/\r\n/g, '\n')
      let index
      while ((index = buffer.indexOf('\n\n')) >= 0) {
        emit(buffer.slice(0, index)); buffer = buffer.slice(index + 2)
      }
    },
    finish() { if (buffer.trim()) emit(buffer); buffer = '' }
  }
}

export async function consumeSSE(response, onEvent) {
  if (!response.ok) throw await responseError(response)
  if (!response.body) throw new Error('流连接不可用')
  const parser = createSSEParser(onEvent), reader = response.body.getReader(), decoder = new TextDecoder()
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      parser.push(decoder.decode(value, { stream: true }))
    }
    parser.push(decoder.decode()); parser.finish()
  } finally { reader.releaseLock() }
}

async function responseError(response) {
  let body
  try { body = await response.json() } catch { body = {} }
  const error = new Error(typeof body.detail === 'string' ? body.detail : `请求失败（${response.status}）`)
  error.status = response.status
  return error
}

export const operationKey = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`

export function createFusionClient({ tokenProvider = () => globalThis.localStorage?.getItem('token'), fetchImpl = globalThis.fetch, base = '/api/v1' } = {}) {
  async function request(path, method = 'GET', body, key, stream = false, signal) {
    const token = tokenProvider()
    const headers = { 'Content-Type': 'application/json' }
    if (token) headers.Authorization = 'Bearer ' + token
    if (key) headers['Idempotency-Key'] = key
    const response = await fetchImpl(base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal })
    if (!response.ok) throw await responseError(response)
    return stream ? response : response.json()
  }
  return {
    knowledge: () => request('/knowledge/active'),
    createSession: () => request('/consultations', 'POST', {}),
    sessions: () => request('/consultations'),
    getSession: id => request('/consultations/' + encodeURIComponent(id)),
    async message(id, message, onEvent, signal) { return consumeSSE(await request(`/consultations/${encodeURIComponent(id)}/messages`, 'POST', { message }, null, true, signal), onEvent) },
    createInsight: (body, key = operationKey()) => request('/insights', 'POST', body, key),
    createTask: (body, key = operationKey()) => request('/content-tasks', 'POST', body, key),
    tasks: () => request('/content-tasks'),
    task: id => request('/content-tasks/' + encodeURIComponent(id)),
    actOnTask: (id, body, key = operationKey()) => request(`/content-tasks/${encodeURIComponent(id)}/actions`, 'POST', body, key),
    async events(id, onEvent, signal) { return consumeSSE(await request(`/content-tasks/${encodeURIComponent(id)}/events`, 'GET', undefined, null, true, signal), onEvent) },
    draftWiki: (id, body) => request(`/content-tasks/${encodeURIComponent(id)}/wiki-draft`, 'POST', body),
    approveWiki: (id, body, key = operationKey()) => request(`/content-tasks/${encodeURIComponent(id)}/wiki-review`, 'POST', body, key),
    assets: () => request('/assets'),
    publish: (asset, key = operationKey()) => request(`/assets/${asset.asset_id}/publish`, 'POST', { expected_state_version: asset.state_version }, key),
    revoke: (asset, key = operationKey()) => request(`/assets/${asset.asset_id}/revoke`, 'POST', { expected_state_version: asset.state_version }, key),
    handoff: () => request('/voice/handoff', 'POST', {})
  }
}
