import test from 'node:test'
import assert from 'node:assert/strict'
import { initialTaskView, applyTaskEvent, normalizeTask, draftInsightFromTurn, turnContentAction, canCreateInsight, canPublishAsset } from './fusion-state.js'

test('咨询转线索只预填脱敏问题，必须人工确认', () => {
  const draft = draftInsightFromTurn({ turn_id: 't1', question: '联系 13800138000 或 a@example.com，SC-PB27多重？', answer: '用户声称只有100g' })
  assert.equal(draft.turn_id, 't1')
  assert.equal(draft.confirmed, false)
  assert.doesNotMatch(draft.summary, /13800138000|a@example.com|100g/)
  assert.equal(canCreateInsight(draft), false)
  assert.equal(canCreateInsight({ ...draft, confirmed: true }), true)
  assert.equal(canCreateInsight({ ...draft, confirmed: true, summary: '   ' }), false)
})

test('只有可沉淀的模型补答显示 Wiki 入口，旧版咨询保留内容任务', () => {
  assert.equal(turnContentAction({ answer_route: 'model_assist', wiki_candidate: true }), 'model_wiki')
  assert.equal(turnContentAction({ answer_route: 'model_assist', wiki_candidate: false }), null)
  assert.equal(turnContentAction({ answer_route: 'model_assist' }), null)
  assert.equal(turnContentAction({ answer_route: 'assistant_intro' }), null)
  assert.equal(turnContentAction({ answer_route: 'rag' }), 'content')
  assert.equal(turnContentAction({}), 'content')
})

test('兼容任务扁平字段与原工作流values，不丢来源和事实报告', () => {
  const task = normalizeTask({ task_id: 'a', state_version: 2, source: { insight_id: 'i' }, values: { article_content: '正文', generated_topics: ['主题'], fact_check_report: { status: 'pass' } } })
  assert.equal(task.article_content, '正文')
  assert.equal(task.source.insight_id, 'i')
  assert.equal(task.fact_check_report.status, 'pass')
  assert.deepEqual(task.generated_topics, ['主题'])
})

test('SSE快照更新任务，错误任务与过期状态不污染当前视图', () => {
  let view = initialTaskView('a')
  view = applyTaskEvent(view, { type: 'state_snapshot', payload: { task_id: 'a', run_id: 'r1', state_version: 3, article_version_id: 'v1', article_content: '已保存' } })
  const saved = view
  assert.equal(view.article_content, '已保存')
  assert.equal(applyTaskEvent(view, { type: 'state_snapshot', payload: { task_id: 'other', state_version: 99 } }), saved)
  assert.equal(applyTaskEvent(view, { type: 'state_snapshot', payload: { task_id: 'a', state_version: 2, article_content: '旧稿' } }), saved)
})

test('不同run、版本和检查JSON不得混入正文，事件重复不会重复Token', () => {
  let view = applyTaskEvent(initialTaskView('a'), { type: 'state_snapshot', data: { task_id: 'a', run_id: 'r1', state_version: 3, article_version_id: 'v1', article_content: '' } })
  const token = { type: 'article_delta', event_id: 'e1', task_id: 'a', run_id: 'r1', article_version_id: 'v1', state_version: 3, payload: { content: '正文' } }
  view = applyTaskEvent(view, token)
  assert.equal(view.article_content, '正文')
  assert.equal(applyTaskEvent(view, token).article_content, '正文')
  view = applyTaskEvent(view, { ...token, event_id: 'e2', run_id: 'old', payload: { content: '错误run' } })
  view = applyTaskEvent(view, { ...token, event_id: 'e3', article_version_id: 'old', payload: { content: '旧稿' } })
  view = applyTaskEvent(view, { type: 'fact_check', task_id: 'a', run_id: 'r1', state_version: 3, payload: { status: 'conflict', checks: [{ reason: '错误数值' }] } })
  assert.equal(view.article_content, '正文')
  assert.equal(view.fact_check_report.status, 'conflict')
})

test('明确新写稿版本后清空旧正文，再接收新版本Token', () => {
  let view = applyTaskEvent(initialTaskView('a'), { type: 'state_snapshot', payload: { task_id: 'a', run_id: 'r2', state_version: 5, article_version_id: 'v1', article_content: '旧正文' } })
  view = applyTaskEvent(view, { type: 'node_status', task_id: 'a', run_id: 'r2', state_version: 5, article_version_id: 'v2', payload: { node: 'write_draft', status: 'running' } })
  view = applyTaskEvent(view, { type: 'article_delta', task_id: 'a', run_id: 'r2', state_version: 5, article_version_id: 'v2', payload: { content: '新正文' } })
  assert.equal(view.article_content, '新正文')
  assert.equal(view.article_version_id, 'v2')
})

test('发布资格独立于图片成功，事实冲突或旧知识版本时禁止发布', () => {
  const approved = { text_review_status: 'approved', publication_status: 'draft', fact_check_report: { status: 'pass' }, image_status: 'failed', knowledge_snapshot_id: 'k1' }
  assert.equal(canPublishAsset(approved, 'k1'), true)
  assert.equal(canPublishAsset({ ...approved, text_review_status: 'pending' }, 'k1'), false)
  assert.equal(canPublishAsset({ ...approved, fact_check_report: { status: 'needs_review' } }, 'k1'), false)
  assert.equal(canPublishAsset(approved, 'k2'), false)
  assert.equal(canPublishAsset({ ...approved, publication_status: 'published' }, 'k1'), false)
})

test('a delayed task refresh cannot replace another task or roll back a newer state', async () => {
  const { mergeTaskSnapshot } = await import('./fusion-state.js')
  const current = { task_id: 'next', state_version: 5, article_content: 'streamed text' }
  assert.equal(mergeTaskSnapshot(current, { task_id: 'previous', state_version: 8 }), current)
  assert.equal(mergeTaskSnapshot(current, { task_id: 'next', state_version: 4 }), current)
  assert.equal(mergeTaskSnapshot(current, { task_id: 'next', state_version: 5 }), current)
  assert.equal(mergeTaskSnapshot(null, { task_id: 'next', state_version: 6 }), null)
  const updated = mergeTaskSnapshot(current, { task_id: 'next', state_version: 6, values: { article_content: 'saved text' } })
  assert.equal(updated.article_content, 'saved text')
})

test('approved asset navigation matches the current task and approved version', async () => {
  const { approvedAssetForTask } = await import('./fusion-state.js')
  const task = { task_id: 'task-a', article_version_id: 'v2' }
  const assets = [{ task_id: 'task-b', version_id: 'v2' }, { task_id: 'task-a', version_id: 'v1' }]
  assert.equal(approvedAssetForTask(assets, task), undefined)
  const expected = { task_id: 'task-a', version_id: 'v2' }
  assert.equal(approvedAssetForTask([...assets, expected], task), expected)
})
