// Line-level LCS diff. No model call and no HTML rendering of article content.
export function lineDiff(before = '', after = '') {
  const a = before.split('\n'), b = after.split('\n')
  if (a.length * b.length > 250000) {
    return [{ type: 'removed', text: before }, { type: 'added', text: after }]
  }
  const dp = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1))
  for (let i = a.length - 1; i >= 0; i--) {
    for (let j = b.length - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? 1 + dp[i + 1][j + 1] : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out = []
  let i = 0, j = 0
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) out.push({ type: 'same', text: a[i++] }), j++
    else if (i < a.length && (j === b.length || dp[i + 1][j] >= dp[i][j + 1])) out.push({ type: 'removed', text: a[i++] })
    else out.push({ type: 'added', text: b[j++] })
  }
  return out
}

export function versionView(task, selectedId) {
  if (!task) return { content: '', previous: null, historical: false }
  const versions = task.article_versions
  const selectedIndex = versions.findIndex(version => version.version_id === selectedId)
  const currentIndex = versions.findIndex(version => version.version_id === task.article_version_id)
  const index = selectedIndex >= 0 ? selectedIndex : currentIndex
  const historical = selectedIndex >= 0 && selectedIndex !== currentIndex
  return {
    content: historical ? versions[index].content : task.article_content,
    previous: index < 0 ? versions.at(-1)?.content ?? null : versions[index - 1]?.content ?? null,
    historical
  }
}
