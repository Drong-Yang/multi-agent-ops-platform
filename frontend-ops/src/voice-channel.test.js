import test from 'node:test'
import assert from 'node:assert/strict'
import { createFusionClient } from './fusion-api.js'
import { openVoiceChannel, voiceChannelNotice } from './voice-channel.js'

const ready = { ready: true, reason: 'ready' }

test('演示模式和未就绪的语音入口不打开页面、不申请交换码', async () => {
  for (const reason of ['mock_mode', 'configuration', 'voice_ui_unavailable']) {
    const availability = { ready: false, reason }
    assert.match(voiceChannelNotice(availability), /start-voice\.cmd/)
    assert.equal(await openVoiceChannel(availability, {
      openWindow: () => assert.fail('语音未就绪时不打开页面'),
      handoff: () => assert.fail('语音未就绪时不申请交换码')
    }), false)
  }
})

test('就绪时保留用户触发的弹窗，只将一次性交换码放在语音页的片段中', async () => {
  const events = []
  const tab = { opener: {}, location: { replace: url => events.push(url) } }
  assert.equal(await openVoiceChannel(ready, {
    openWindow: (url, target) => { events.push([url, target]); return tab },
    handoff: () => { events.push('handoff'); return Promise.resolve({ code: 'vh1.test+code' }) }
  }), true)
  assert.equal(tab.opener, null)
  assert.deepEqual(events, [['about:blank', '_blank'], 'handoff', 'http://127.0.0.1:5179/#handoff=vh1.test%2Bcode'])
})

test('语音服务在点击后退出时，关闭空白页面并保留后端错误', async () => {
  const unavailable = Object.assign(new Error('语音页面尚未就绪'), { status: 503 })
  let closed = false
  await assert.rejects(openVoiceChannel(ready, {
    openWindow: () => ({ close: () => { closed = true }, location: { replace: () => assert.fail('不应导航') } }),
    handoff: () => Promise.reject(unavailable)
  }), unavailable)
  assert.equal(closed, true)
})

test('现有登录凭据只发给工作台 API，申请交换码时重新核验语音就绪', async () => {
  const requests = []
  const client = createFusionClient({
    tokenProvider: () => 'test-operator-token',
    fetchImpl: async (url, options) => {
      requests.push({ url, ...options })
      return Response.json(url.includes('availability') ? ready : { code: 'vh1.test' })
    }
  })
  assert.deepEqual(await client.voiceAvailability(), ready)
  await client.handoff()
  assert.deepEqual(requests.map(({ url, method }) => [url, method]), [
    ['/api/v1/voice/availability', 'GET'], ['/api/v1/voice/handoff?require_ready=true', 'POST']
  ])
  assert.equal(requests.every(request => request.headers.Authorization === 'Bearer test-operator-token'), true)
})
