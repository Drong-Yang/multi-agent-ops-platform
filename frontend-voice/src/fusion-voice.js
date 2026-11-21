/** Voice-only capability bootstrap. Never reads the operator's login token. */
const VOICE_TOKEN_KEY = 'fusion_voice_token';
const FUSION_API_BASE = 'http://127.0.0.1:8010';

function voiceHeaders(storage) {
  const token = storage.getItem(VOICE_TOKEN_KEY);
  if (!token) throw new Error('语音授权不可用，请从运营工作台重新打开语音渠道。');
  return { Authorization: `Bearer ${token}` };
}

async function initializeVoiceHandoff({ location, history, storage, fetchImpl, apiBase = FUSION_API_BASE }) {
  const fragment = new URLSearchParams((location.hash || '').replace(/^#/, ''));
  const code = fragment.get('handoff');
  if (code !== null) {
    // Clear the one-time code before starting a network request or rendering RTC.
    fragment.delete('handoff');
    const remaining = fragment.toString();
    history.replaceState(null, '', `${location.pathname || '/'}${location.search || ''}${remaining ? `#${remaining}` : ''}`);
    storage.removeItem(VOICE_TOKEN_KEY);
    if (!code || code.length > 512) throw new Error('语音授权无效，请重新从运营工作台打开。');
    let response;
    try {
      response = await fetchImpl(`${apiBase.replace(/\/$/, '')}/api/v1/voice/exchange`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code }),
        credentials: 'omit',
        cache: 'no-store',
      });
    } catch (_) {
      throw new Error('无法连接融合服务，请确认服务运行后重新从运营工作台打开。');
    }
    if (!response.ok) throw new Error('语音授权已过期或已使用，请重新从运营工作台打开。');
    let body;
    try { body = await response.json(); } catch (_) { throw new Error('服务未返回有效语音授权。'); }
    const token = body.access_token || body.token;
    if (typeof token !== 'string' || !token.trim()) throw new Error('服务未返回有效语音授权。');
    storage.setItem(VOICE_TOKEN_KEY, token);
  }
  return Boolean(storage.getItem(VOICE_TOKEN_KEY));
}

module.exports = { VOICE_TOKEN_KEY, FUSION_API_BASE, voiceHeaders, initializeVoiceHandoff };
