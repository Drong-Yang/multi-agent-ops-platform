// Isolated Mock acceptance for the visible asset -> consultation return path.
// Refuses a real model or Milvus endpoint before sending any test questions.
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const apiBase = process.env.FUSION_API || 'http://127.0.0.1:8012';
const webBase = process.env.FUSION_WEB || 'http://127.0.0.1:5180';
const root = path.resolve(__dirname, '..');
const report = { mode: 'mock_only', checks: [], errors: [] };

async function checked(response) {
  if (!response.ok) throw new Error('Local fixture request failed: ' + response.status);
  return response.json();
}

(async () => {
  const out = path.join(root, 'evals', 'runs', 'faq-reuse-browser');
  await fs.mkdir(out, { recursive: true });
  let browser;
  try {
    for (const base of [apiBase, webBase]) if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) throw new Error('Only loopback test servers are permitted');
    const health = await checked(await fetch(apiBase + '/health'));
    if (health.mock !== true) throw new Error('This acceptance only runs with Mock generation');
    const credentials = { username: 'faq_ui_' + crypto.randomBytes(5).toString('hex'), password: crypto.randomBytes(12).toString('hex') };
    await checked(await fetch(apiBase + '/api/v1/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(credentials) }));
    const token = (await checked(await fetch(apiBase + '/api/v1/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(credentials) }))).access_token;
    const headers = { 'Content-Type': 'application/json', Authorization: 'Bearer ' + token };
    const get = async endpoint => checked(await fetch(apiBase + '/api/v1' + endpoint, { headers }));
    const post = async (endpoint, body) => checked(await fetch(apiBase + '/api/v1' + endpoint, { method: 'POST', headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() }, body: JSON.stringify(body) }));
    const knowledge = await get('/knowledge/active');
    if (knowledge.retrieval_mode !== 'sample') throw new Error('This acceptance requires sample retrieval, not paid Embedding');
    const fixtureSession = await post('/consultations', {});
    const question = 'SC-PB27 多重？';
    const reply = await fetch(apiBase + `/api/v1/consultations/${fixtureSession.session_id}/messages`, { method: 'POST', headers, body: JSON.stringify({ message: question }) });
    if (!reply.ok) throw new Error('Fixture consultation failed');
    await reply.text();
    const turn = (await get('/consultations/' + fixtureSession.session_id)).turns.at(-1);
    const insight = await post('/insights', { session_id: fixtureSession.session_id, turn_id: turn.turn_id, summary: question, confirmed: true });
    let task = await post('/content-tasks', { insight_id: insight.insight_id, content_type: 'faq', product_scope: ['SC-PB27'], content_brief: { min_chars: 50, max_chars: 1200, audience: '选购者' } });
    async function waitTask(identity, status) {
      const deadline = Date.now() + 30000;
      while (Date.now() < deadline) {
        const state = await get('/content-tasks/' + identity);
        if (state.status === status) return state;
        if (['failed', 'interrupted'].includes(state.status)) throw new Error('Fixture content task failed');
        await new Promise(resolve => setTimeout(resolve, 150));
      }
      throw new Error('Fixture content task timed out');
    }
    task = await waitTask(task.task_id, 'waiting_human');
    await post(`/content-tasks/${task.task_id}/actions`, { action: 'select_topic', data: { selected_topic: task.values.generated_topics[0] }, expected_state_version: task.state_version });
    task = await waitTask(task.task_id, 'waiting_human');
    await post(`/content-tasks/${task.task_id}/actions`, { action: 'approve_text', data: {}, expected_state_version: task.state_version });
    task = await waitTask(task.task_id, 'text_approved');
    let asset = (await get('/assets')).assets.find(a => a.task_id === task.task_id);
    await post(`/assets/${asset.asset_id}/publish`, { expected_state_version: asset.state_version });
    asset = (await get('/assets')).assets.find(a => a.asset_id === asset.asset_id);
    if (!asset.reuse?.eligible) throw new Error('Published fixture asset not ready for reuse');

    browser = await chromium.launch({ headless: true, executablePath: process.env.FUSION_BROWSER || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe' });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    page.on('pageerror', error => report.errors.push(error.message));
    let browserMessages = 0;
    page.on('request', request => { if (request.method() === 'POST' && /\/consultations\/[^/]+\/messages(?:\?|$)/.test(request.url())) browserMessages++; });
    await page.goto(webBase);
    await page.getByLabel('用户名', { exact: true }).fill(credentials.username);
    await page.getByLabel('密码', { exact: true }).fill(credentials.password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('heading', { name: '商品与售后咨询' }).waitFor();
    await page.locator('.fusion-nav nav button').nth(2).click();
    const assetCard = page.locator('#asset-' + asset.asset_id);
    await assetCard.getByText('可参与咨询匹配', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(out, 'asset-reuse-conditions.png'), fullPage: true });
    const beforeMessages = browserMessages;
    const beforeSessions = (await get('/consultations')).sessions.length;
    await assetCard.getByRole('button', { name: '去客服验证', exact: true }).click();
    await page.getByLabel('客服素材验证').waitFor();
    if ((await page.getByLabel('咨询问题').inputValue()) !== asset.reuse.sample_question) throw new Error('Asset sample question was not prefilled');
    if (browserMessages !== beforeMessages || await page.locator('.turn').count()) throw new Error('Verification unexpectedly sent a question');
    const afterSessions = (await get('/consultations')).sessions;
    if (afterSessions.length !== beforeSessions + 1 || afterSessions[0]?.turn_count) throw new Error('Verification did not create one blank text session');
    report.checks.push('asset eligibility, scope and sample question are visible; verification creates a blank session without sending');

    await page.getByRole('button', { name: '发送问题', exact: true }).click();
    await page.getByText('本次参考了已发布 FAQ 素材', { exact: true }).waitFor({ timeout: 15000 });
    await page.getByRole('button', { name: '查看这份素材', exact: true }).waitFor();
    if (browserMessages !== beforeMessages + 1) throw new Error('Explicit send did not issue exactly one consultation');
    const state = await get('/consultations/' + afterSessions[0].session_id);
    const selected = state.turns.at(-1);
    if (!selected.faq_asset_ids.includes(asset.asset_id) || !selected.faq_reuse.selected_assets.some(a => a.asset_id === asset.asset_id)) throw new Error('Detailed FAQ trace did not identify the fixture asset');
    await page.screenshot({ path: path.join(out, 'consultation-selected-asset.png'), fullPage: true });
    report.checks.push('explicit send selects the published asset and shows its excerpt and match reason');

    await page.getByRole('button', { name: '查看这份素材', exact: true }).click();
    await page.locator('#asset-' + asset.asset_id + '.asset-highlighted').waitFor();
    await page.locator('#asset-' + asset.asset_id).getByRole('button', { name: '撤回素材', exact: true }).click();
    await page.getByText('已撤回', { exact: true }).waitFor();
    if (!(await page.locator('#asset-' + asset.asset_id).getByRole('button', { name: '去客服验证', exact: true }).isDisabled())) throw new Error('Revoked asset still offers verification');
    await page.locator('.fusion-nav nav button').first().click();
    await page.getByLabel('咨询问题').fill(asset.reuse.sample_question);
    await page.getByRole('button', { name: '发送问题', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.turn').length === 2 && document.querySelector('.message-form button')?.textContent.includes('发送问题'), null, { timeout: 15000 });
    const afterRevoke = (await get('/consultations/' + afterSessions[0].session_id)).turns.at(-1);
    if (afterRevoke.faq_asset_ids.includes(asset.asset_id)) throw new Error('Revoked asset was still selected');
    const latest = page.locator('.turn').last();
    await latest.locator('.reuse-trace summary').click();
    await latest.getByText(afterRevoke.faq_reuse.message, { exact: true }).waitFor();
    report.checks.push('trace links back to the source asset; revoke disables verification and a later question excludes it with an explanation');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(out, 'reuse-mobile.png'), fullPage: true });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2)) throw new Error('FAQ traces overflow mobile layout');
    report.checks.push('390px trace layout without horizontal overflow');
    report.success = report.errors.length === 0;
  } catch (error) {
    report.success = false;
    report.error = String(error);
  } finally {
    if (browser) await browser.close();
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify(report));
  process.exitCode = report.success ? 0 : 1;
})();
