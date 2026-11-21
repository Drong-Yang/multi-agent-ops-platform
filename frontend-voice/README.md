# 电商运营 Agent 语音渠道

从用户提供的智能客服 React 前端白名单迁移，保留火山 RTC SDK、入会/离会、字幕、插话和原页面交互。上游许可见 `LICENSE` 与 `THIRD_PARTY_NOTICES`。原目录、原 node_modules、构建产物、`.env`、`.npmrc` 和运行数据均未迁入。

## 本副本的接入方式

- 融合后端默认 `http://127.0.0.1:8010`，所有旧 schema 请求只转发至 `/api/v1/voice/legacy`，不连接独立端口 3001。
- 运营工作台先通过登录 JWT 调用 `POST /api/v1/voice/handoff`，再打开 `http://127.0.0.1:5179/#handoff=<一次性兑换码>`。JWT 不放入地址。
- 页面在渲染 RTC 前立即移除片段中的兑换码，调用 `/api/v1/voice/exchange`；只将响应中的限时语音 capability 保存为 `sessionStorage.fusion_voice_token`。
- 每个场景与代理请求携带该 capability；缺失、过期或返回 401/403 时，需要重新从工作台打开。页面不读取运营 JWT 或 localStorage 登录凭据。
- 默认不自动开始通话。只有用户开始操作才使用原 SDK 流程。

## 运行与验证

```powershell
npm ci
npm run test:fusion
npm run build
npm start
```

前端监听本机 5179，后端监听 8010；跨端口请求由后端 CORS 配置允许。可用 `REACT_APP_FUSION_API_BASE` 显式覆盖融合 API 地址。开发与构建命令在新目录运行，不安装或写入来源项目。

`src/fusion-voice.test.cjs` 验证一次性 code 移除顺序、兑换、cap 头、刷新、缺少授权和兑换失败；这些离线测试不证明 RTC 云服务、麦克风、ASR/TTS、插话或挂断重连已完成真实验收。真实语音验收仍按项目 Roadmap 留证。
