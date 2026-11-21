/**
 * Copyright 2025 Beijing Volcano Engine Technology Co., Ltd. All Rights Reserved.
 * SPDX-license-identifier: BSD-3-Clause
 */

import ReactDOM from 'react-dom/client';
import { Provider } from 'react-redux';
import App from './App';
import store from './store';
import './index.less';
import fusionVoice from './fusion-voice.js';
import { FUSION_API_BASE } from './config';

const { initializeVoiceHandoff } = fusionVoice;

const root = ReactDOM.createRoot(document.getElementById('root') as HTMLElement);
async function bootstrap() {
  root.render(<div style={{ padding: 32 }}>正在连接语音客服…</div>);
  try {
    const authorized = await initializeVoiceHandoff({
      location: window.location,
      history: window.history,
      storage: window.sessionStorage,
      fetchImpl: window.fetch.bind(window),
      apiBase: FUSION_API_BASE,
    });
    if (!authorized) throw new Error('请先登录运营工作台，再点击“打开语音渠道”。');
    root.render(<Provider store={store}><App /></Provider>);
  } catch (error) {
    const message = error instanceof Error ? error.message : '语音授权失败，请重新从运营工作台打开。';
    root.render(<main style={{ maxWidth: 680, margin: '64px auto', padding: 24, lineHeight: 1.8 }}>
      <h1 style={{ fontSize: 24 }}>实时语音客服</h1>
      <p role="alert">{message}</p>
      <p><a href="http://127.0.0.1:5178/">返回运营工作台</a></p>
    </main>);
  }
}

bootstrap();
