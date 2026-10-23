// 端到端 UI 生成验证：填表→生成→核对结果卡片（预览灯箱已删除）
const path = require('path')
const PW = 'C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core'
const { chromium } = require(PW)

const OUT = process.argv[2] || '.tmp-shots/ui-gen'
const results = []
function check(name, cond, extra = '') {
  results.push({ ok: !!cond })
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  -> ' + extra : ''}`)
}

async function main() {
  const fs = require('fs')
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 420, height: 720 }, deviceScaleFactor: 2 })
  const errs = []
  page.on('pageerror', (e) => errs.push(e.message))

  await page.goto('http://127.0.0.1:5173/', { waitUntil: 'networkidle' })
  await page.waitForTimeout(1200)

  const author = `UI验证${Date.now() % 1000}`
  const title = '灯箱下线后界面复核'
  await page.fill('input[placeholder="例如：张三"]', author)
  await page.fill('input[placeholder="例如：基于深度学习的图像分割方法研究"]', title)
  await page.screenshot({ path: path.join(OUT, '1-filled.png') })

  await page.click('button:has-text("生成录用图")')
  await page.waitForTimeout(2500)
  await page.screenshot({ path: path.join(OUT, '2-progress.png') })
  const progressVisible = await page.locator('.progress').isVisible().catch(() => false)
  check('生成中有进度条', progressVisible)
  const noPreviewDuringRun = (await page.locator('.preview, .lightbox').count()) === 0
  check('进度条下方无预览/灯箱', noPreviewDuringRun)

  // 等结果卡片（最长 120s）
  await page.waitForSelector('.result.ok, .result.fail', { timeout: 120000 })
  await page.waitForTimeout(600)
  await page.screenshot({ path: path.join(OUT, '3-result.png'), fullPage: false })

  const okCard = await page.locator('.result.ok').count()
  const fname = okCard ? (await page.locator('.result.ok .fname').innerText()).trim() : ''
  check('生成成功并显示文件名', okCard === 1 && !!fname, fname)
  check('结果卡片图片可点击放大已移除', (await page.locator('.preview').count()) === 0)
  check('灯箱元素不存在', (await page.locator('.lightbox').count()) === 0)
  check('保留打开文件夹/复制路径', (await page.locator('button:has-text("打开文件夹")').count()) === 1 && (await page.locator('button:has-text("复制路径")').count()) === 1)

  // 快捷键与提示条：结果态下速查条应隐藏
  const tipsInResult = await page.locator('.tips').count()
  check('结果态隐藏速查条', tipsInResult === 0, String(tipsInResult))

  check('无运行时错误', errs.length === 0, errs.join(' | '))

  await browser.close()
  const failed = results.filter((r) => !r.ok).length
  console.log(`\n${results.length - failed}/${results.length} 通过`)
  process.exit(failed ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(1) })
