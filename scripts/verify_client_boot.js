/* 启动竞态验证：sidecar 未就绪 → 错误卡片 + 重试按钮；起来后点重试能恢复 */
const { chromium } = require('C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core');
const { spawn, execSync } = require('child_process');

const BASE = 'http://localhost:5173';
const REPO = 'E:/psluyong-lite';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // 1. 先把 8765 上的 sidecar 杀掉
  try {
    const out = execSync('netstat -ano | grep :8765 | grep LISTENING', { shell: 'bash' }).toString();
    const pid = out.trim().split(/\s+/).pop();
    execSync(`taskkill //F //PID ${pid}`, { shell: 'bash' });
    console.log('killed_sidecar_pid=' + pid);
  } catch {
    console.log('killed_sidecar_pid=none');
  }
  await sleep(800);

  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 560, height: 820 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));

  // 2. 打开页面：8 次退避重试约 11 秒后应停在错误卡片
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  const bootingText = await page.locator('.empty').first().innerText().catch(() => '');
  console.log('booting_text=' + JSON.stringify(bootingText.replace(/\s+/g, ' ').trim()));

  await page.waitForSelector('.result.fail', { timeout: 20000 });
  const failText = (await page.locator('.result.fail').first().innerText()).replace(/\s+/g, ' ').trim();
  const retryVisible = await page.locator('button:has-text("重试连接")').count();
  console.log('fail_text=' + JSON.stringify(failText));
  console.log('retry_btn=' + retryVisible);
  await page.screenshot({ path: 'E:/psluyong-lite/.workbuddy/tmp/shots/boot_fail.png' });

  // 3. 起 sidecar，点「重试连接」，应自动进入正常界面
  const child = spawn(`${REPO}/.venv/Scripts/python.exe`, ['-m', 'client.sidecar.server'], {
    cwd: REPO,
    stdio: 'ignore',
    detached: false,
  });
  await sleep(5000);
  await page.click('button:has-text("重试连接")');
  await sleep(4000);

  const stillFail = await page.locator('.result.fail').count();
  const hasTabs = await page.locator('.seg button').count();
  const recordBtn = await page.locator('button:has-text("录用")').count();
  console.log('after_retry_fail=' + stillFail + ' tabs=' + hasTabs + ' record_btn=' + recordBtn);
  await page.screenshot({ path: 'E:/psluyong-lite/.workbuddy/tmp/shots/boot_recovered.png' });

  console.log('js_errors=' + JSON.stringify(errors));
  await browser.close();
  child.kill();
})();
