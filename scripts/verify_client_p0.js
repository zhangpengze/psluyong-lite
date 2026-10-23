/* 客户端改动验证：历史页区间/统计/刷新 + 回填已移除 + 无控制台错误 */
const { chromium } = require('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core');

const BASE = 'http://localhost:5173';
const SHOT = 'E:/psluyong-lite/.workbuddy/tmp/shots';

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

  // 1. 切到记录页
  await page.click('button:has-text("记录")');
  await page.waitForSelector('.hitem, .empty', { timeout: 8000 });
  const statLabel = await page.locator('.stat-pill .sp-label').first().innerText();
  const statValue = await page.locator('.stat-pill .sp-value').first().innerText();
  const chips = await page.locator('.range-chip').allInnerTexts();
  const activeChip = await page.locator('.range-chip.active').innerText();
  console.log('stat_label=' + JSON.stringify(statLabel));
  console.log('stat_value=' + JSON.stringify(statValue));
  console.log('chips=' + JSON.stringify(chips));
  console.log('active_chip=' + JSON.stringify(activeChip));

  // 2. 回填按钮应该已经不存在
  console.log('fill_btn_count=' + (await page.locator('button:has-text("回填")').count()));

  // 3. 搜索框右侧两个圆钮不重叠
  const refreshBox = await page.locator('.refresh-btn').boundingBox();
  const searchBox = await page.locator('.search input').boundingBox();
  console.log('refresh_btn_visible=' + !!refreshBox + ' inside_search=' + (refreshBox && searchBox && refreshBox.x + refreshBox.width <= searchBox.x + searchBox.width + 1));

  // 4. 切「全部」→ 条数应该变大（服务端 7 天窗口被绕开）
  await page.click('.range-chip:has-text("全部")');
  await page.waitForTimeout(1600);
  const allValue = await page.locator('.stat-pill .sp-value').first().innerText();
  console.log('all_value=' + JSON.stringify(allValue));
  console.log('range_more=' + (await page.locator('.range-more').count()));

  // 5. 手动刷新：列表不闪骨架屏（刷新期间 .hitem 仍在）
  await page.click('.refresh-btn');
  await page.waitForTimeout(250);
  const duringRefresh = await page.locator('.hitem').count();
  await page.waitForTimeout(1500);
  const afterRefresh = await page.locator('.hitem').count();
  console.log('items_during_refresh=' + duringRefresh + ' after=' + afterRefresh);

  await page.screenshot({ path: SHOT + '/history_range.png', fullPage: false });

  // 6. 生成页：作者框应从 last_author 带出（保存过才有值，没有则空/草稿）
  await page.click('button:has-text("录用")');
  await page.waitForTimeout(600);
  const authorVal = await page.locator('input[placeholder="例如：张三"]').inputValue();
  console.log('author_prefilled=' + JSON.stringify(authorVal));
  await page.screenshot({ path: SHOT + '/record_page.png' });

  console.log('js_errors=' + JSON.stringify(errors));
  await browser.close();
})();
