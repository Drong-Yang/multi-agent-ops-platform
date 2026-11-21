const assert = require('node:assert/strict')
const { randomUUID } = require('node:crypto')
const fs = require('node:fs/promises')
const path = require('node:path')
const { chromium } = require('playwright')

const root = path.resolve(__dirname, '..')
const api = process.env.FUSION_API || 'http://127.0.0.1:8012'
const web = process.env.FUSION_WEB || 'http://127.0.0.1:5180'
const out = path.join(root, 'evals', 'runs', 'support-wiki')
const question = 'SC-PB999 无法连接怎么办？'

async function main() {
  assert.equal(new URL(api).hostname, '127.0.0.1')
  assert.equal(new URL(web).hostname, '127.0.0.1')
  const health = await (await fetch(api + '/health')).json()
  assert(health.mock && health.retrieval_mode === 'sample', 'Use the isolated Mock preview')
  const credentials = { username: 'wiki_ui_' + randomUUID().slice(0, 10), password: randomUUID() }
  const result = await fetch(api + '/api/v1/auth/register', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(credentials)
  })
  assert(result.ok)
  await fs.mkdir(out, { recursive: true })
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.FUSION_BROWSER || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
  })
  const checks = []
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } })
  try {
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    const button = name => page.getByRole('button', { name, exact: true })
    const field = name => page.getByLabel(name, { exact: true })
    const ask = async question => {
      await field('咨询问题').fill(question)
      await button('发送问题').click()
    }
    await page.goto(web)
    await field('用户名').fill(credentials.username)
    await field('密码').fill(credentials.password)
    await button('登录').click()
    await field('咨询会话').locator('option').first().waitFor({ state:'attached' })
    console.log('Workbench ready')
    await ask('SC-PB27 多重？')
    await page.getByText('引用商品原文 · 跳过回答模型', { exact: true }).waitFor()
    assert.match(await page.locator('.turn').last().innerText(), /568g/)
    checks.push('catalog_quote')
    console.log('Catalog quoted')
    await ask(question)
    await page.getByText('待核实 · 资料不足 · 跳过回答模型', { exact: true }).waitFor()
    await page.locator('.turn').last().getByRole('button', { name: '转为内容任务 →' }).click()
    await field('内容类型').selectOption('wiki')
    await page.getByRole('checkbox', { name: /移除不必要/ }).check()
    await button('创建内容任务').click()
    console.log('Wiki task submitted')
    await field('资料名称').fill('虚构设备维修手册')
    await field('HTTPS 来源地址').fill('https://support.example.com/guide')
    await field('原始资料摘录').fill('应用连接出现异常时，请重启配套应用后重新配对。')
    await button('用所选资料生成 Wiki 草稿').click()
    await page.waitForFunction(() => document.querySelector('textarea[rows="3"]')?.value.includes('重新配对'))
    await page.getByText('Wiki 草稿已预填，可核对或修改后批准。', { exact: true }).waitFor()
    assert.equal(await field('标准问题').inputValue(), question)
    const preservedScope = await field('适用范围').inputValue()
    assert(preservedScope.trim())
    const edited = '请先重启配套应用，再重新配对。'
    assert.notEqual(await field('标准答复').inputValue(), edited)
    await field('标准答复').fill(edited)
    await button('批准 Wiki 草稿').click()
    await button('查看最近审核版本').click()
    const asset = page.locator('.asset-highlighted')
    assert.equal(await asset.locator('.wiki-customer-answer .prose').innerText(), edited)
    assert(!(await asset.getByText(preservedScope, { exact: true }).isVisible()))
    await asset.getByText('适用范围与处理步骤', { exact: true }).click()
    assert(await asset.getByText(preservedScope, { exact: true }).isVisible())
    await button('发布为售后 Wiki').click()
    console.log('Wiki publication submitted')
    await button('去客服验证').click()
    assert.equal(await field('咨询问题').inputValue(), question)
    assert.equal(await page.locator('.turn').count(), 0)
    await button('发送问题').click()
    await page.getByText('直接复用售后 Wiki · 跳过回答模型', { exact: true }).waitFor()
    assert.equal(await page.locator('.turn').last().locator('.answer > p').innerText(), edited)
    checks.push('edited_standard_answer_matches_asset_and_customer_reply')
    await page.screenshot({ path: path.join(out, 'consultation.png'), animations: 'disabled' })
    await button('查看本轮 Wiki 条目').click()
    await page.locator('.asset-highlighted').getByRole('button', { name: '撤回 Wiki' }).click()
    await page.getByText('已撤回', { exact: true }).waitFor()
    await page.getByRole('button', { name: '客服试聊', exact: true }).click()
    await ask(question)
    await page.locator('.turn').last().getByText('待核实 · 资料不足 · 跳过回答模型', { exact: true }).waitFor()
    checks.push('revoke_falls_back')
    await page.setViewportSize({ width: 390, height: 844 })
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth))
    checks.push('mobile')
    await page.setViewportSize({ width: 1400, height: 1000 })
    const returnsQuestion = '耳机拆封后能退吗？'
    let draftRequests = 0
    page.on('request', request => { if (request.url().endsWith('/wiki-draft')) draftRequests++ })
    await ask(returnsQuestion)
    await page.locator('.turn').last().getByText('原始资料检索', { exact: true }).waitFor()
    await page.locator('.turn').last().getByRole('button', { name: '转为内容任务 →' }).click()
    await field('内容类型').selectOption('wiki')
    await page.getByRole('checkbox', { name: /移除不必要/ }).check()
    await button('创建内容任务').click()
    await page.getByText('Wiki 草稿已预填，可核对或修改后批准。', { exact: true }).waitFor()
    assert.equal(await field('标准问题').inputValue(), returnsQuestion)
    assert.equal(draftRequests, 1)
    await page.reload()
    await page.getByRole('button', { name: /^内容任务/ }).click()
    await page.locator('.task-item').filter({ hasText: returnsQuestion }).click()
    await page.getByText('Wiki 草稿已预填，可核对或修改后批准。', { exact: true }).waitFor()
    assert.equal(draftRequests, 1)
    await button('批准 Wiki 草稿').click()
    await button('查看最近审核版本').click()
    await button('发布为售后 Wiki').waitFor()
    checks.push('auto_prefill_persists_and_approves_original_returns_question')
    assert.deepEqual(errors, [])
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify({ passed: true, checks }, null, 2))
    console.log(checks.join(' | '))
  } catch (error) {
    await page.screenshot({ path: path.join(out, 'failure.png') }).catch(() => {})
    throw error
  } finally {
    await browser.close()
  }
}
main().catch(error => {
  console.error(error.message)
  process.exitCode = 1
})
