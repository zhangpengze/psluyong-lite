/* P2 验证：设置页版本/诊断、草稿提示只弹一次、记录页仍正常 */
const { chromium } = require('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core');

const BASE = 'http://localhost:5173';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 560, height: 820 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console: ' + m.text());
  });

  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);

  // 1. 设置页：版本行 + 诊断复制
  await page.click('button:has-text("设置")');
  await page.waitForSelector('.diag-row', { timeout: 5000 });
  const diagText = (await page.locator('.diag-ver').innerText()).trim();
  console.log('diag_version=' + JSON.stringify(diagText));
  await page.click('a:has-text("复制诊断信息")');
  await page.waitForTimeout(600);
  const toasts = await page.locator('.toast').allInnerTexts();
  console.log('copy_toast=' + JSON.stringify(toasts.map((t) => t.replace(/\s+/g, ' ').trim())));
  const clip = await page.evaluate(() => navigator.clipboard.readText().catch(() => ''));
  console.log('clipboard_head=' + JSON.stringify(String(clip).split('\n').slice(0, 2)));
  await page.screenshot({ path: 'E:/psluyong-lite/.workbuddy/tmp/shots/settings_diag.png' });

  // 2. 草稿提示只弹一次
  await page.click('button:has-text("录用")');
  await page.waitForTimeout(400);
  await page.fill('input[placeholder="例如：张三"]', '草稿验证甲');
  await sleep(900); // 等草稿落盘
  await page.click('button:has-text("记录")');
  await page.waitForTimeout(300);
  await page.click('button:has-text("录用")');
  await page.waitForTimeout(500);
  const firstRound = await page.locator('.toast').allInnerTexts();
  console.log('first_round_toast=' + JSON.stringify(firstRound.map((t) => t.replace(/\s+/g, ' ').trim())));

  await sleep(3600); // 等 toast 自动消失
  await page.click('button:has-text("记录")');
  await page.waitForTimeout(300);
  await page.click('button:has-text("录用")');
  await page.waitForTimeout(500);
  const secondRound = await page.locator('.toast').count();
  console.log('second_round_toast_count=' + secondRound);
  const authorKept = await page.locator('input[placeholder="例如：张三"]').inputValue();
  console.log('draft_restored_value=' + JSON.stringify(authorKept));

  // 清掉测试草稿
  await page.evaluate(() => localStorage.removeItem('psluyong-lite.draft.record'));

  console.log('js_errors=' + JSON.stringify(errors));
  await browser.close();
})();
