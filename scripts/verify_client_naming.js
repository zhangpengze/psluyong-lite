/* 验证：设置页「图片命名」两个选项、保存落盘、刷新后仍保持 */
const { chromium } = require('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core');

const BASE = 'http://localhost:5173';
const SIDECAR = 'http://127.0.0.1:8765';

async function readConfig() {
  const res = await fetch(SIDECAR + '/config');
  return (await res.json()).config;
}

(async () => {
  const before = await readConfig();
  console.log('config_before=' + JSON.stringify(before.filename_rule));

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 560, height: 900 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console: ' + m.text());
  });

  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  await page.click('button:has-text("设置")');
  await page.waitForSelector('.opt-row', { timeout: 5000 });

  const labels = await page.locator('.opt-row .opt-name').allInnerTexts();
  const samples = await page.locator('.opt-row .opt-sample').allInnerTexts();
  const checkedBefore = await page.locator('.opt-row.active .opt-name').innerText();
  console.log('options=' + JSON.stringify(labels));
  console.log('samples=' + JSON.stringify(samples));
  console.log('checked_before=' + JSON.stringify(checkedBefore));
  console.log('save_disabled_before=' + (await page.locator('button:has-text("保存设置")').isDisabled()));

  // 切到「作者姓名」→ 按钮应变可用
  await page.click('.opt-row:has-text("作者姓名")');
  await page.waitForTimeout(300);
  console.log('save_disabled_after_switch=' + (await page.locator('button:has-text("保存设置")').isDisabled()));
  await page.locator('.opt-row').first().scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'E:/psluyong-lite/.workbuddy/tmp/shots/naming_options.png' });

  await page.click('button:has-text("保存设置")');
  await page.waitForTimeout(900);
  const toasts = (await page.locator('.toast').allInnerTexts()).map((t) => t.replace(/\s+/g, ' ').trim());
  console.log('save_toast=' + JSON.stringify(toasts));

  const saved = await readConfig();
  console.log('config_after_save=' + JSON.stringify(saved.filename_rule));

  // 刷新后仍保持选中
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForTimeout(1200);
  await page.click('button:has-text("设置")');
  await page.waitForSelector('.opt-row', { timeout: 5000 });
  const checkedAfterReload = await page.locator('.opt-row.active .opt-name').innerText();
  console.log('checked_after_reload=' + JSON.stringify(checkedAfterReload));

  // 恢复默认，别把验证用的选择留给用户
  await page.click('.opt-row:has-text("作者 + 标题")');
  await page.waitForTimeout(200);
  await page.click('button:has-text("保存设置")');
  await page.waitForTimeout(800);
  const restored = await readConfig();
  console.log('config_restored=' + JSON.stringify(restored.filename_rule));

  console.log('js_errors=' + JSON.stringify(errors));
  await browser.close();
})();
