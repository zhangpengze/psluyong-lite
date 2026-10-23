/* 批次二浏览器级验证：详情抽屉 + toast + 快捷日期 + 导出按钮 */
const { chromium } = require('playwright-core');

const BASE = 'http://127.0.0.1:8080';
const USER = process.env.PS_USER;
const PASS = process.env.PS_PASS;

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });

  await page.goto(BASE + '/login');
  await page.fill('#username', USER);
  await page.fill('#password', PASS);
  await page.click('button[type=submit]');
  await page.waitForURL('**/dashboard');

  // 1. 记录页：chips / 导出按钮 / toast.js 就位
  await page.goto(BASE + '/records');
  await page.waitForSelector('tr.row-click');
  const chipActive = await page.locator('.chip.active').innerText();
  const exportHref = await page.locator('a.btn:has-text("导出 CSV")').getAttribute('href');
  console.log('chip_active=' + JSON.stringify(chipActive));
  console.log('export_href=' + exportHref);

  // 2. 点第一行 → 抽屉展开，字段渲染
  const firstRow = page.locator('tr.row-click').first();
  await firstRow.click();
  await page.waitForSelector('tr.detail-row', { timeout: 5000 });
  const detailText = await page.locator('tr.detail-row').innerText();
  const hasFields = ['提交机器', 'PS 版本', '耗时', '图片路径'].every(k => detailText.includes(k));
  console.log('drawer_open=' + (await page.locator('tr.detail-row').count() > 0));
  console.log('drawer_fields=' + hasFields);

  // 3. 点第二行 → 抽屉切换（只有一个开着）
  await page.locator('tr.row-click').nth(1).click();
  await page.waitForTimeout(400);
  console.log('drawer_switch_count=' + await page.locator('tr.detail-row').count());

  // 4. ESC 收起
  await page.keyboard.press('Escape');
  await page.waitForTimeout(200);
  console.log('drawer_after_esc=' + await page.locator('tr.detail-row').count());

  // 5. toast 渲染与自动消失配置
  await page.evaluate(() => toast('浏览器验证 toast', 'success'));
  await page.waitForSelector('.toast.success', { timeout: 3000 });
  console.log('toast_visible=' + await page.locator('.toast.success').count());

  // 6. 导出链接真实可下载
  const dl = await Promise.all([
    page.waitForEvent('download', { timeout: 8000 }),
    page.click('a.btn:has-text("导出 CSV")'),
  ]).then(r => r[0]).catch(() => null);
  console.log('download=' + (dl ? dl.suggestedFilename() : 'FAILED'));

  console.log('js_errors=' + (errors.length ? errors.join(' | ') : 'none'));
  await browser.close();
})().catch(e => { console.error('FATAL: ' + e.message); process.exit(1); });
