export function normalizeTask(task = {}) {
  const values = task.values || {}
  return { ...values, ...task, source: task.source || {}, values,
    article_content: task.article_content ?? values.article_content ?? '',
    generated_topics: task.generated_topics ?? values.generated_topics ?? [],
    article_versions: task.article_versions ?? values.article_versions ?? [],
    fact_check_report: task.fact_check_report ?? values.fact_check_report ?? {},
    quality_report: task.quality_report ?? values.quality_report ?? {},
    image_urls: task.image_urls ?? values.image_urls ?? [],
    article_version_id: task.article_version_id || task.current_version_id || values.article_versions?.at(-1)?.version_id || '' }
}

export function initialTaskView(id) { return { ...normalizeTask({ task_id: id, state_version: 0 }), seen: [] } }

export function applyTaskEvent(view, event) {
  const payload = event.payload ?? event.data ?? {}
  if ((event.task_id || payload.task_id) && (event.task_id || payload.task_id) !== view.task_id) return view
  if (event.event_id && view.seen?.includes(event.event_id)) return view
  const version = event.state_version ?? payload.state_version
  if (version !== undefined && version < view.state_version) return view
  if (event.type !== 'state_snapshot' && event.run_id && view.run_id && event.run_id !== view.run_id) return view
  let next = { ...view, seen: [...(view.seen || []), ...(event.event_id ? [event.event_id] : [])].slice(-512) }
  if (version !== undefined) next.state_version = version
  if (event.type === 'state_snapshot') return { ...next, ...normalizeTask(payload), seen: next.seen }
  if (event.type === 'node_status') {
    next.node = payload.node
    if (payload.node === 'write_draft' && event.article_version_id && event.article_version_id !== view.article_version_id) {
      next.article_version_id = event.article_version_id; next.article_content = ''
    }
  } else if (event.type === 'article_delta') {
    if (event.article_version_id && view.article_version_id && event.article_version_id !== view.article_version_id) return view
    next.article_version_id = event.article_version_id || view.article_version_id
    next.article_content += payload.content || ''
  } else if (event.type === 'fact_check') next.fact_check_report = payload
  else if (event.type === 'run_failed') next.error = payload.error || payload.message || '执行失败，请查看已保存状态'
  return next
}

export function draftInsightFromTurn(turn) {
  const question = turn.question || turn.content || ''
  return { turn_id: turn.turn_id, summary: question.replace(/1[3-9]\d{9}/g, '[手机号已移除]').replace(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/g, '[邮箱已移除]'), confirmed: false }
}

export function turnContentAction(turn) {
  if (turn.answer_route === 'assistant_intro') return null
  if (turn.answer_route === 'model_assist') return turn.wiki_candidate === true ? 'model_wiki' : null
  return 'content'
}

export const canCreateInsight = draft => !!(draft?.confirmed && draft.turn_id && draft.summary?.trim())
export const canPublishAsset = (asset, active) => !!(asset.text_review_status === 'approved' && asset.publication_status === 'draft' &&
  asset.knowledge_snapshot_id === active && asset.fact_check_report?.passed !== false && ['pass', 'passed'].includes(asset.fact_check_report?.status))

export function mergeTaskSnapshot(current, snapshot) {
  if (!current || current.task_id !== snapshot.task_id || snapshot.state_version <= current.state_version) return current
  return normalizeTask(snapshot)
}

export function approvedAssetForTask(assets, task) {
  return assets.find(asset => asset.task_id === task.task_id && asset.version_id === task.article_version_id)
}
