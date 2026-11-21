// Live RTC acceptance with synthetic audio and a dedicated test account.
const assert = require('node:assert/strict')
const { randomBytes } = require('node:crypto')
const fs = require('node:fs/promises')
const path = require('node:path')

const root = path.resolve(__dirname, '..')
const workbench = 'http://127.0.0.1:5178'
const api = 'http://127.0.0.1:8010'
const out = path.join(root, 'evals', 'runs', 'delivery-live.json')
const report = { started_at: new Date().toISOString(), checks: [], voice_calls: [] }

async function writeReport() {
  await fs.writeFile(out, JSON.stringify(report, null, 2), 'utf8')
}

function result(name, passed, details = {}) {
  report.checks.push({ name, passed, ...details })
  console.log((passed ? 'PASS ' : 'FAIL ') + name)
}

async function json(endpoint, options = {}) {
  const response = await fetch(api + endpoint, { ...options, signal: AbortSignal.timeout(12000) })
  assert(response.ok, endpoint.replace(/[a-f0-9]{24,}/g, '[id]') + ' HTTP ' + response.status)
  return response.json()
}

function instrumentVoice(wav) {
  if (!navigator.mediaDevices || !window.RTCPeerConnection) return
  const peers = new Set()
  const Peer = window.RTCPeerConnection
  window.RTCPeerConnection = class extends Peer {
    constructor(...args) {
      super(...args)
      peers.add(this)
    }
  }

  const getMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
  let mixer, destination
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    const stream = await getMedia(constraints)
    if (!constraints?.audio) return stream
    mixer = new AudioContext()
    destination = mixer.createMediaStreamDestination()
    await mixer.resume()
    for (const track of stream.getAudioTracks()) {
      stream.removeTrack(track)
      track.stop()
    }
    stream.addTrack(destination.stream.getAudioTracks()[0])
    return stream
  }
  window.testSpeak = async () => {
    if (!mixer || !destination) return false
    const bytes = Uint8Array.from(atob(wav), character => character.charCodeAt(0))
    const source = mixer.createBufferSource()
    source.buffer = await mixer.decodeAudioData(bytes.buffer)
    source.connect(destination)
    source.start()
    return true
  }
  window.testAudio = async () => {
    let packets = 0, energy = 0
    for (const peer of peers) {
      if (peer.connectionState === 'closed') continue
      for (const row of (await peer.getStats()).values()) {
        if (row.type === 'inbound-rtp' && (row.kind || row.mediaType) === 'audio') {
          packets += row.packetsReceived || 0
          energy += row.totalAudioEnergy || 0
        }
      }
    }
    return { packets, energy }
  }
}

async function waitForVoiceTurn(headers, seen) {
  const deadline = Date.now() + 36000
  while (Date.now() < deadline) {
    const { sessions } = await json('/api/v1/consultations', { headers })
    for (const session of sessions.filter(item => item.channel === 'voice' && item.turn_count > (seen.get(item.session_id) || 0))) {
      const detail = await json('/api/v1/consultations/' + session.session_id, { headers })
      const turn = detail.turns.at(-1)
      if (turn) return { session, turn }
    }
    await new Promise(resolve => setTimeout(resolve, 850))
  }
  throw new Error('No completed consultation was saved from RTC')
}

async function runCall(voice, headers, seen) {
  const leave = voice.getByAltText('leave', { exact: true })
  await voice.getByText('通话', { exact: true }).click({ timeout: 12000 })
  try {
    await leave.waitFor({ timeout: 26000 })
    await voice.getByText('麦克风：已开启', { exact: true }).waitFor({ timeout: 15000 })
    await voice.waitForFunction(async () => (await window.testAudio()).packets > 0, null, { timeout: 22000 })
    const interrupt = voice.getByText('点此打断', { exact: true })
    if (await interrupt.isVisible()) await interrupt.click()
    const statsBefore = await voice.evaluate(() => window.testAudio())
    assert(await voice.evaluate(() => window.testSpeak()), 'Could not queue synthetic microphone input')
    const { session, turn } = await waitForVoiceTurn(headers, seen)
    seen.set(session.session_id, session.turn_count)
    await voice.waitForFunction(async (start) => {
      const stats = await window.testAudio()
      return stats.packets > start.packets + 4 && stats.energy > start.energy + .00001
    }, statsBefore, { timeout: 13000 })
    const statsAfter = await voice.evaluate(() => window.testAudio())
    const recognized = /耳机.*(?:开封|拆封)|(?:开封|拆封).*退/.test(turn.question)
    const grounded = /退|售后/.test(turn.answer) && turn.sources?.length > 0
    const receivedSpeech = statsAfter.packets > statsBefore.packets && statsAfter.energy > statsBefore.energy
    report.voice_calls.push({ recognized, grounded, receivedSpeech, input: turn.question, answer: turn.answer,
      source_count: turn.sources?.length || 0, packets: statsAfter.packets,
      answered_audio_energy: statsAfter.energy - statsBefore.energy })
    return { session, turn, passed: recognized && grounded && receivedSpeech }
  } finally {
    if (await leave.isVisible()) {
      await leave.click()
      await voice.getByText('通话', { exact: true }).waitFor({ timeout: 12000 })
      assert.equal(await voice.getByText('挂断清理未完成', { exact: true }).count(), 0, 'Cloud hangup was not confirmed')
    }
  }
}

async function main() {
  assert(process.argv.includes('--run'), 'Explicit --run is required for real cloud calls.')
  const health = await json('/health')
  assert(health.status === 'healthy' && health.mock === false && health.retrieval_mode === 'milvus',
    'Start the live voice stack before testing.')
  const manifest = JSON.parse(await fs.readFile(path.join(root, 'frontend-voice', 'build', 'asset-manifest.json'), 'utf8'))
  assert(!Object.keys(manifest.files).some(name => name.startsWith('static/media/fusion-voice.')),
    'Voice authorization must be compiled into JavaScript instead of emitted as a static asset.')
  const wav = (await fs.readFile(path.join(root, '.runtime', 'synthetic-question.wav'))).toString('base64')
  const credentials = { username: 'rtc_delivery_' + randomBytes(6).toString('hex'), password: randomBytes(16).toString('hex') }
  const request = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(credentials) }
  await json('/api/v1/auth/register', request)
  const { access_token: token } = await json('/api/v1/auth/login', request)
  const headers = { Authorization: 'Bearer ' + token }
  const { chromium } = require('playwright')
  report.stage = 'launch_browser'
  await writeReport()
  const browser = await chromium.launch({
    executablePath: process.env.FUSION_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    headless: true,
    args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream', '--autoplay-policy=no-user-gesture-required']
  })
  try {
    report.stage = 'open_workbench'
    await writeReport()
    const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, permissions: ['microphone'] })
    await context.addInitScript(instrumentVoice, wav)
    const page = await context.newPage()
    await page.goto(workbench)
    await page.getByLabel('用户名', { exact: true }).fill(credentials.username)
    await page.getByLabel('密码', { exact: true }).fill(credentials.password)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.getByRole('heading', { name: '商品与售后咨询' }).waitFor()
    await page.screenshot({ path: path.join(root, 'evals', 'runs', 'delivery-workbench.png'), animations: 'disabled' })

    const popup = page.waitForEvent('popup')
    await page.getByRole('button', { name: '打开语音渠道' }).click()
    const voice = await popup
    await voice.bringToFront()
    const seen = new Map()
    report.stage = 'first_voice_call'
    await writeReport()
    const first = await runCall(voice, headers, seen)
    result('rtc_connection_asr_tts_and_hangup', first.passed)
    await writeReport()
    report.stage = 'reconnect'
    await writeReport()
    const second = await runCall(voice, headers, seen)
    result('rtc_reconnect_with_fresh_uplink_and_response', second.passed)
    await writeReport()
    await voice.close()

    report.stage = 'voice_to_content_task'
    await writeReport()
    await page.getByRole('button', { name: '刷新会话' }).click()
    await page.getByLabel('咨询会话').locator('option[value="' + second.session.session_id + '"]').waitFor({ state: 'attached' })
    await page.getByLabel('咨询会话').selectOption(second.session.session_id)
    const latest = page.locator('.turn').last()
    await latest.getByRole('button', { name: '转为内容任务 →' }).click()
    await page.getByRole('checkbox').check()
    await page.getByRole('button', { name: '创建内容任务' }).click()
    await page.getByRole('button', { name: '查看来源咨询 ↗' }).click({ timeout: 25000 })
    await page.locator('.turn.linked-turn').waitFor()
    result('voice_consultation_to_content_task_and_backlink', second.turn.sources?.length > 0)
  } finally {
    await browser.close()
  }
  report.stage = 'completed'
  if (report.checks.some(check => !check.passed)) process.exitCode = 1
}

main().catch(error => {
  const firstLine = String(error.message).split('\n')[0].replace(/https?:\/\/\S+/g, '[URL]')
  result('verification_error', false, { detail: firstLine })
  console.error(firstLine)
  process.exitCode = 1
}).finally(async () => {
  report.finished_at = new Date().toISOString()
  await writeReport()
})
