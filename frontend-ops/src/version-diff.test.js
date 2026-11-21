import test from 'node:test'
import assert from 'node:assert/strict'
import { lineDiff } from './version-diff.js'
test('preserves common lines and identifies additions/deletions', () => {
  const result = lineDiff('案例甲\n旧结构\n结尾', '案例甲\n三步清单\n结尾')
  assert.deepEqual(result, [
    { type: 'same', text: '案例甲' }, { type: 'removed', text: '旧结构' },
    { type: 'added', text: '三步清单' }, { type: 'same', text: '结尾' }])
  assert.equal(result.filter(x => x.type !== 'added').map(x => x.text).join('\n'), '案例甲\n旧结构\n结尾')
  assert.equal(result.filter(x => x.type !== 'removed').map(x => x.text).join('\n'), '案例甲\n三步清单\n结尾')
})

test('compares the displayed version to its predecessor, including a streamed current draft', async () => {
  const { versionView } = await import('./version-diff.js')
  const versions = [{ version_id: 'v1', content: 'one' }, { version_id: 'v2', content: 'two' }, { version_id: 'v3', content: 'three' }]
  const task = { article_versions: versions, article_version_id: 'v3', article_content: 'three' }
  assert.deepEqual(versionView(task, 'v1'), { content: 'one', previous: null, historical: true })
  assert.deepEqual(versionView(task, 'v2'), { content: 'two', previous: 'one', historical: true })
  assert.deepEqual(versionView(task, 'v3'), { content: 'three', previous: 'two', historical: false })
  assert.deepEqual(versionView(task, ''), { content: 'three', previous: 'two', historical: false })
  assert.deepEqual(versionView({ ...task, article_version_id: 'pending', article_content: 'partial' }, ''),
    { content: 'partial', previous: 'three', historical: false })
})
