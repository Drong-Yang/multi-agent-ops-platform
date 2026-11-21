// Browser acceptance against the local app; uses synthetic account/questions only.
const { chromium } = require('playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const apiBase = process.env.FUSION_API || 'http://127.0.0.1:8010';
const webBase = process.env.FUSION_WEB || 'http://127.0.0.1:5178';

(async () => {
  const out = path.join(root, 'evals', 'runs', 'browser');
  await fs.mkdir(out, { recursive: true });
  const report = { checks: [], errors: [] };
  const browser = await chromium.launch({ headless: true, executablePath: process.env.FUSION_BROWSER || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe' });
  try {
    const healthResponse = await fetch(apiBase + '/health');
    const health = await healthResponse.json();
    if (!healthResponse.ok || health.mock !== true || health.retrieval_mode !== 'sample') throw new Error('UI smoke requires isolated Mock generation and sample retrieval');
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.on('pageerror', e => report.errors.push(e.message));
    const username = 'browser_' + crypto.randomBytes(5).toString('hex');
    const password = crypto.randomBytes(12).toString('hex');
    const registered = await fetch(apiBase + '/api/v1/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) });
    if (!registered.ok) throw new Error('browser fixture registration failed');
    await page.goto(webBase);
    await page.getByLabel('用户名', { exact: true }).fill(username);
    await page.getByLabel('密码', { exact: true }).fill(password);
    await page.getByRole('button', { name: '登录', exact: true }).click();
    await page.getByRole('heading', { name: '商品与售后咨询' }).waitFor();
    report.checks.push('login and new workspace');
    await page.getByLabel('咨询问题').fill('SC-PB27 多重？');
    await page.getByRole('button', { name: '发送问题', exact: true }).click();
    await page.locator('.turn .answer > p').filter({ hasText: '568' }).waitFor({ timeout: 15000 });
    await page.getByRole('button', { name: '转为内容任务 →', exact: true }).click();
    await page.getByRole('checkbox').check();
    await page.screenshot({ path: path.join(out, 'consultation-bridge.png'), fullPage: true });
    await page.getByRole('button', { name: '创建内容任务', exact: true }).click();
    await page.locator('.topics button').first().waitFor({ timeout: 20000 });
    await page.locator('.topics button').first().click();
    const approve = page.getByRole('button', { name: '批准当前文本', exact: true });
    await approve.waitFor({ timeout: 20000 });
    if (await approve.isDisabled()) throw new Error('valid fixture draft unexpectedly blocked');
    await page.screenshot({ path: path.join(out, 'content-review.png'), fullPage: true });
    report.checks.push('consultation -> confirmed insight -> evidence-aware task -> human review');
    await page.getByPlaceholder('例如保留重量与尺寸，补充资料边界，避免额外性能承诺').fill('补充说明以资料为准。');
    await page.getByRole('button', { name: '携反馈改稿', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('select[aria-label="文章版本"] option').length >= 3, null, { timeout: 20000 });
    await page.getByRole('button', { name: '批准当前文本', exact: true }).waitFor();
    await page.getByRole('button', { name: '批准当前文本', exact: true }).click();
    await page.getByRole('button', { name: '查看内容资产', exact: true }).waitFor({ timeout: 20000 });
    await page.getByRole('button', { name: '查看内容资产', exact: true }).click();
    const publish = page.getByRole('button', { name: '发布为客服素材', exact: true });
    await publish.waitFor();
    // List refresh disables actions briefly; click waits for the actual enabled state.
    await publish.click({ timeout: 15000 });
    await page.getByText('可参与咨询匹配', { exact: true }).waitFor();
    await page.screenshot({ path: path.join(out, 'published-faq.png'), fullPage: true });
    report.checks.push('feedback revision, versions, text approval and separate FAQ publication');
    await page.locator('.fusion-nav nav button').first().click();
    await page.getByLabel('咨询问题').fill('SC-PB27 重量是多少，保修多久？');
    await page.getByRole('button', { name: '发送问题', exact: true }).click();
    await page.getByText('本次参考了已发布 FAQ 素材', { exact: true }).waitFor({ timeout: 15000 });
    report.checks.push('published FAQ actually reused by a later consultation');
    await page.locator('.fusion-nav nav button').nth(2).click();
    await page.getByRole('button', { name: '撤回素材', exact: true }).click();
    await page.getByText('已撤回', { exact: true }).waitFor();
    report.checks.push('FAQ revoked in UI');
    await page.setViewportSize({ width: 390, height: 844 });
    await page.locator('.fusion-nav nav button').first().click();
    await page.screenshot({ path: path.join(out, 'mobile-consultation.png'), fullPage: true });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 2);
    if (overflow) throw new Error('mobile layout overflows horizontally');
    report.checks.push('390px mobile layout without horizontal overflow');
    report.success = report.errors.length === 0;
    if (!report.success) throw new Error('browser raised page errors');
  } catch (e) {
    report.success = false; report.error = String(e);
  } finally {
    await browser.close();
    await fs.writeFile(path.join(out, 'report.json'), JSON.stringify(report, null, 2));
  }
  console.log(JSON.stringify(report));
  process.exitCode = report.success ? 0 : 1;
})();
