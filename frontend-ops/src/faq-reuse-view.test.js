import test from 'node:test'
import assert from 'node:assert/strict'
import { assetReuseView, faqReuseView, createFaqVerification } from './faq-reuse-view.js'

const ready = {
  asset_id: 'asset-ready', content_type: 'faq', publication_status: 'published', knowledge_snapshot_id: 'ks-current',
  reuse: { eligible: true, status: 'ready', message: '已发布，等待相关咨询匹配。', intent_labels: ['重量'], sample_question: 'SC-PB27 多重？' }
}

test('客服验证入口以服务端有效性为准，不能只看发布状态', () => {
  assert.equal(assetReuseView(ready, 'ks-current').canVerify, true)
  const invalid = assetReuseView({ ...ready, reuse: { ...ready.reuse, eligible: false, status: 'invalid_approval', message: '正文或批准版本已变化。' } }, 'ks-current')
  assert.equal(invalid.canVerify, false)
  assert.equal(invalid.message, '正文或批准版本已变化。')
  assert.equal(assetReuseView({ ...ready, knowledge_snapshot_id: 'ks-old' }, 'ks-current').canVerify, false)
  assert.equal(assetReuseView({ ...ready, publication_status: 'revoked' }, 'ks-current').canVerify, false)
})

test('图文资产和旧API不伪装成已可供客服选用', () => {
  const article = assetReuseView({ ...ready, content_type: 'article' }, 'ks-current')
  assert.equal(article.canVerify, false)
  assert.equal(article.status, 'not_faq')
  const legacy = assetReuseView({ ...ready, reuse: undefined }, 'ks-current')
  assert.equal(legacy.canVerify, false)
  assert.match(legacy.message, /重启.*后端/)
})

test('问答保留具体引用资产和未选原因，历史回答不凭空补造匹配依据', () => {
  const view = faqReuseView({ faq_reuse: { status: 'selected', message: '已参考一份素材。',
    selected_assets: [{ asset_id: 'a', title: '重量说明', excerpt: '重568g', match_reason: '同商品、同主题' }],
    candidates: [{ asset_id: 'b', title: '退货说明', code: 'intent_mismatch', reason: '问题主题不同' }] } })
  assert.equal(view.selected[0].asset_id, 'a')
  assert.equal(view.selected[0].match_reason, '同商品、同主题')
  assert.equal(view.candidates[0].reason, '问题主题不同')
  const legacy = faqReuseView({ faq_asset_ids: ['old-asset'] })
  assert.equal(legacy.selected[0].asset_id, 'old-asset')
  assert.match(legacy.message, /历史|未保存/)
  assert.equal(faqReuseView({}).status, 'unknown')
})

test('去客服验证仅创建新文字会话与预填问题，绝不自动发问', async () => {
  const calls = []
  const client = {
    createSession: async () => { calls.push('new-session'); return { session_id: 'session-new', channel: 'text', turns: [] } },
    message: () => { calls.push('paid-message'); throw new Error('must not be called') }
  }
  const result = await createFaqVerification(client, ready, 'ks-current')
  assert.deepEqual(calls, ['new-session'])
  assert.equal(result.session.session_id, 'session-new')
  assert.equal(result.question, 'SC-PB27 多重？')
  assert.equal(result.asset_id, 'asset-ready')
  assert.deepEqual(result.session.turns, [])
})

test('未发布、失效或缺少建议问题时不发起任何验证请求', async () => {
  let calls = 0
  const client = { createSession: async () => { calls++; return {} } }
  for (const asset of [
    { ...ready, publication_status: 'draft' },
    { ...ready, reuse: { ...ready.reuse, eligible: false } },
    { ...ready, reuse: { ...ready.reuse, sample_question: '   ' } }
  ]) await assert.rejects(createFaqVerification(client, asset, 'ks-current'))
  assert.equal(calls, 0)
})
