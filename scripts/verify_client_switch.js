// 页签切换验证：状态保持（keep-alive）、滚动位置记忆、切换方向动画、请求次数
const path = require('path')
const PW = 'C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core'
const { chromium } = require(PW)

const BASE = 'http://127.0.0.1:5173/'
const results = []
function check(name, cond, extra = '') {
  results.push({ name, ok: !!cond, extra })
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${name}${extra ? '  -> ' + extra : ''}`)
}

async function setTab(page, tab) {
  await page.evaluate((t) => { window.location.hash = t }, tab)
  await page.waitForTimeout(420) // 过渡 0.14+0.16s，留余量
}

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 420, height: 720 } })
  const reqs = []
  page.on('request', (r) => {
    if (r.url().includes(':8765/')) reqs.push(`${r.method()} ${new URL(r.url()).pathname}`)
  })
  const errs = []
  page.on('pageerror', (e) => errs.push(e.message))

  await page.goto(BASE, { waitUntil: 'networkidle' })
  await page.waitForTimeout(1000)

  // 1) 录用页：填内容 → 切走 → 切回，内容仍在、不被清空
  await page.fill('input[placeholder="例如：张三"]', '状态保持测试')
  await setTab(page, 'settings')
  await setTab(page, 'record')
  const kept = await page.inputValue('input[placeholder="例如：张三"]')
  check('切页后表单内容保留', kept === '状态保持测试', kept)

  // 2) 设置页：改了没保存 → 切走 → 切回，改动还在（keep-alive）
  await setTab(page, 'settings')
  await page.fill('input[placeholder="例如：张三"]', '未保存改动')
  await setTab(page, 'record')
  await setTab(page, 'settings')
  const unsaved = await page.inputValue('input[placeholder="例如：张三"]')
  check('设置页未保存改动不被丢弃', unsaved === '未保存改动', unsaved)

  // 3) 历史页：滚动位置记忆
  await setTab(page, 'history')
  await page.waitForTimeout(600)
  await page.evaluate(() => { document.querySelector('.body').scrollTop = 400 })
  await page.waitForTimeout(200)
  const before = await page.evaluate(() => document.querySelector('.body').scrollTop)
  await setTab(page, 'settings')
  await setTab(page, 'history')
  const after = await page.evaluate(() => document.querySelector('.body').scrollTop)
  check('切回历史页恢复滚动位置', before > 0 && Math.abs(after - before) < 30, `${before} -> ${after}`)

  // 4) 切到别的页不残留旧滚动位置
  await setTab(page, 'record')
  const recScroll = await page.evaluate(() => document.querySelector('.body').scrollTop)
  check('短页面自动回到顶部', recScroll === 0, String(recScroll))

  // 5) 切换方向：按标签顺序前进用 page-next，回退用 page-prev
  const names = await page.evaluate(async () => {
    // 触发前进/后退切换，收集过渡期间出现的 page-* 类名
    const seen = new Set()
    const obs = new MutationObserver((list) => {
      for (const m of list) {
        const el = m.target
        if (!(el instanceof Element)) continue
        for (const c of Array.from(el.classList)) if (c.startsWith('page-')) seen.add(c)
      }
    })
    obs.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'] })
    window.location.hash = 'settings' // 前进
    await new Promise((r) => setTimeout(r, 300))
    window.location.hash = 'record' // 后退
    await new Promise((r) => setTimeout(r, 300))
    obs.disconnect()
    return Array.from(seen)
  })
  const hasNext = names.some((n) => n.includes('page-next'))
  const hasPrev = names.some((n) => n.includes('page-prev'))
  check('切换方向类名生效（前进/后退）', hasNext && hasPrev, names.join(' '))

  // 6) 切页不再重复拉列表（只算 /history 列表请求；缩略图请求不算）
  const histCalls = reqs.filter((r) => r === 'GET /history')
  check('历史列表未被反复请求', histCalls.length <= 6, `${histCalls.length} 次`)

  // 7) 页面无 JS 报错
  check('无运行时错误', errs.length === 0, errs.join(' | '))

  await browser.close()
  const failed = results.filter((r) => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} 通过`)
  process.exit(failed.length ? 1 : 0)
}

main().catch((e) => { console.error(e); process.exit(1) })
