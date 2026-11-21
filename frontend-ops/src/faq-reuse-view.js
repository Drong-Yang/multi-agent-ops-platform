const messages = {
  draft: '尚未发布为客服素材，审核发布后才能被后续咨询选用。',
  revoked: '已撤回，不再供新的咨询选用；历史问答引用记录保留。',
  stale: '知识版本已变化，需要基于当前资料重新核对并发布。',
  not_faq: '图文内容可用于自媒体发布；只有 FAQ 素材参与客服选用。',
  invalid_approval: '正文、审批或资料依据已变化，当前不供客服选用。'
}

export function assetReuseView(asset = {}, activeSnapshot) {
  const reuse = asset.reuse || {}
  let status = reuse.status
  if (asset.content_type !== 'faq') status = 'not_faq'
  else if (asset.publication_status !== 'published') status = asset.publication_status || 'draft'
  else if (activeSnapshot && asset.knowledge_snapshot_id !== activeSnapshot) status = 'stale'
  const question = typeof reuse.sample_question === 'string' ? reuse.sample_question.trim().slice(0, 3000) : ''
  const eligible = status === 'ready' && reuse.eligible === true
  return {
    status: status || 'unknown',
    eligible,
    canVerify: eligible && Boolean(question),
    message: (reuse.status === status && reuse.message) || messages[status] || '请重启融合后端以读取当前客服匹配条件；发布状态本身不代表已经被问答选用。',
    intent_labels: Array.isArray(reuse.intent_labels) ? reuse.intent_labels.filter(x => typeof x === 'string') : [],
    question
  }
}

export function faqReuseView(turn = {}) {
  const trace = turn.faq_reuse
  if (trace && typeof trace === 'object') {
    return { status: trace.status || 'unknown', message: trace.message || '本轮未保存详细选用说明。',
      selected: Array.isArray(trace.selected_assets) ? trace.selected_assets.filter(a => a?.asset_id) : [],
      candidates: Array.isArray(trace.candidates) ? trace.candidates.filter(a => a?.asset_id) : [] }
  }
  const ids = [...new Set(Array.isArray(turn.faq_asset_ids) ? turn.faq_asset_ids.filter(Boolean) : [])]
  return { status: ids.length ? 'selected' : 'unknown',
    message: ids.length ? '历史记录显示本轮参考了已发布 FAQ；当时未保存详细匹配说明。' : '这是旧版问答记录，未保存当时的 FAQ 选用过程；可用新问题验证当前状态。',
    selected: ids.map(id => ({ asset_id: id, title: 'FAQ 素材 · ' + String(id).slice(0, 8) })), candidates: [] }
}

export async function createFaqVerification(client, asset, activeSnapshot) {
  const view = assetReuseView(asset, activeSnapshot)
  if (!view.canVerify) throw new Error(view.eligible ? '暂无建议问题，请在客服页手动输入相关咨询。' : view.message)
  // Creating the blank text session is deliberately the only network operation.
  // The user decides whether and when to submit the prefilled question.
  const session = await client.createSession()
  return { session, question: view.question, asset_id: asset.asset_id,
    title: (asset.content || '').split('\n').find(line => line.trim())?.replace(/^\s*#+\s*/, '').slice(0, 100) || 'FAQ 素材' }
}
