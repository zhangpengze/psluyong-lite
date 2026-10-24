/* 界面密度体检：量各窗口"内容高度 / 窗口高度"、各类元件尺寸、横向占位是否浪费。
   node scripts/measure_layout.js
   依赖：假 sidecar（127.0.0.1:8765）+ vite dev（127.0.0.1:5173） */
const PW = 'C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core'
const { chromium } = require(PW)

const WINDOWS = [
  { route: 'main', w: 360, h: 252 },
  { route: 'history', w: 840, h: 640 },
  { route: 'settings', w: 640, h: 540 },
]

/** 与 App.vue 的 naturalMainHeight 同一套算法：算出主窗"该有多高" */
const NATURAL = () => {
  const intrinsic = (el) => {
    const cs = getComputedStyle(el)
    if (el.classList.contains('stack')) {
      const gap = parseFloat(cs.rowGap || cs.gap) || 0
      let h = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
      let n = 0
      for (const kid of el.children) h += intrinsic(kid) + (n++ ? gap : 0)
      return h
    }
    return el.scrollHeight
  }
  const appEl = document.querySelector('.app')
  const bodyEl = document.querySelector('.body')
  if (!appEl || !bodyEl) return 0
  const cs = getComputedStyle(bodyEl)
  const gap = parseFloat(cs.rowGap || cs.gap) || 0
  let h = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
  let n = 0
  for (const kid of bodyEl.children) {
    if (getComputedStyle(kid).position === 'absolute') continue
    h += intrinsic(kid) + (n++ ? gap : 0)
  }
  for (const kid of appEl.children) {
    if (kid === bodyEl || getComputedStyle(kid).position === 'absolute') continue
    h += kid.scrollHeight
  }
  return Math.ceil(h)
}

const PROBE = () => {
  const px = (n) => Math.round(n * 10) / 10
  const box = (sel) => {
    const el = document.querySelector(sel)
    if (!el) return null
    const r = el.getBoundingClientRect()
    const cs = getComputedStyle(el)
    return {
      h: px(r.height),
      w: px(r.width),
      top: px(r.top),
      pad: cs.padding,
      gap: cs.rowGap === 'normal' ? null : cs.rowGap,
      font: cs.fontSize,
    }
  }
  const all = (sel) =>
    Array.from(document.querySelectorAll(sel)).map((el) => {
      const r = el.getBoundingClientRect()
      return { h: px(r.height), w: px(r.width), top: px(r.top), bottom: px(r.bottom) }
    })
  const body = document.querySelector('.body')
  const spans = all('.stack > *')
  const lastBottom = spans.length ? Math.max(...spans.map((s) => s.bottom)) : 0
  return {
    win: { w: innerWidth, h: innerHeight },
    body: { scrollH: body?.scrollHeight, clientH: body?.clientHeight },
    /** 内容底边到窗口底边还空多少 */
    tailGap: px(innerHeight - lastBottom),
    header: box('.header'),
    ctl: box('.ctl'),
    stackKids: spans.length,
    stackHeights: spans.map((s) => s.h),
    // 记录窗
    statsRow: box('.stats-row'),
    tools: box('.rec-tools'),
    filters: box('.rec-filters'),
    recHeadRows: all('.rec-head').length,
    dateGroups: all('.date-group').length,
    visibleRows: (() => {
      const rows = Array.from(document.querySelectorAll('.rec-row'))
      return rows.filter((r) => r.getBoundingClientRect().bottom <= innerHeight).length
    })(),
    // 设置窗
    cards: all('.card.h3, .card').length,
    fields: all('.field').length,
    saveBar: box('.save-bar'),
  }
}

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  for (const win of WINDOWS) {
    const page = await browser.newPage({ viewport: { width: win.w, height: win.h } })
    await page.goto(`http://127.0.0.1:5173/#/${win.route}`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1800)
    const m = await page.evaluate(PROBE)
    const natural = win.route === 'main' ? await page.evaluate(NATURAL) : null
    console.log(`\n===== ${win.route} (${win.w}x${win.h}) =====`)
    if (natural) {
      console.log(
        `  自适应高度 = ${natural}px（窗口 ${win.h} → 死白 ${win.h - natural}px，占 ${Math.round(((win.h - natural) / win.h) * 100)}%）`,
      )
    }
    console.log(JSON.stringify(m, null, 1))
    await page.close()
  }
  await browser.close()
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
