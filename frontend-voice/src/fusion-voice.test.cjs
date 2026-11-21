const test = require('node:test');
const assert = require('node:assert/strict');
const { initializeVoiceHandoff, voiceHeaders, VOICE_TOKEN_KEY, FUSION_API_BASE } = require('./fusion-voice.js');

function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return { getItem: key => data.get(key) || null, setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) };
}

test('立即移除片段中的一次性code，再交换只限语音的cap，不发送运营JWT', async () => {
  const storage = fakeStorage();
  const order = [];
  const location = { hash: '#handoff=code-1', pathname: '/', search: '' };
  const ok = await initializeVoiceHandoff({ location, history: { replaceState: (_, __, url) => order.push(['clean', url]) }, storage,
    fetchImpl: async (url, options) => { order.push(['fetch', url, options]); return Response.json({ access_token: 'voice-cap' }); } });
  assert.equal(ok, true);
  assert.equal(order[0][0], 'clean');
  assert.equal(order[0][1], '/');
  assert.equal(order[1][1], `${FUSION_API_BASE}/api/v1/voice/exchange`);
  assert.equal(order[1][2].headers.Authorization, undefined);
  assert.deepEqual(JSON.parse(order[1][2].body), { code: 'code-1' });
  assert.equal(storage.getItem(VOICE_TOKEN_KEY), 'voice-cap');
  assert.deepEqual(voiceHeaders(storage), { Authorization: 'Bearer voice-cap' });
});

test('页面刷新只使用sessionStorage已交换cap，不重放code', async () => {
  const storage = fakeStorage({ [VOICE_TOKEN_KEY]: 'old-cap' });
  const ok = await initializeVoiceHandoff({ location: { hash: '', pathname: '/', search: '' }, history: { replaceState() { throw new Error('不应改URL'); } }, storage,
    fetchImpl: () => { throw new Error('不应重新交换'); } });
  assert.equal(ok, true);
  assert.equal(voiceHeaders(storage).Authorization, 'Bearer old-cap');
});

test('无handoff或cap时不能调用RTC业务API，不从长期登录Token回退', async () => {
  const storage = fakeStorage({ token: 'operator-jwt' });
  assert.equal(await initializeVoiceHandoff({ location: { hash: '' }, history: {}, storage, fetchImpl: () => { throw new Error('不应联网'); } }), false);
  assert.throws(() => voiceHeaders(storage), /运营工作台/);
});

test('code过期/重复兑换后清除旧cap并明确失败', async () => {
  const storage = fakeStorage({ [VOICE_TOKEN_KEY]: 'old-cap' });
  await assert.rejects(initializeVoiceHandoff({ location: { hash: '#handoff=expired', pathname: '/', search: '' }, history: { replaceState() {} }, storage,
    fetchImpl: async () => Response.json({ detail: '兑换码已过期' }, { status: 401 }) }), /重新从运营工作台/);
  assert.equal(storage.getItem(VOICE_TOKEN_KEY), null);
});

test('响应缺少cap时拒绝启用，不把成功HTTP当授权成功', async () => {
  await assert.rejects(initializeVoiceHandoff({ location: { hash: '#handoff=code', pathname: '/', search: '' }, history: { replaceState() {} }, storage: fakeStorage(),
    fetchImpl: async () => Response.json({ ok: true }) }), /语音授权/);
});
