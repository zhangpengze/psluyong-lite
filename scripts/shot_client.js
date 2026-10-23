// 三窗布局截图核对：node scripts/shot_client.js [outDir]
// 依赖：假 sidecar（127.0.0.1:8765）+ vite dev（127.0.0.1:5173）
// 每个窗口按真实窗口尺寸截图，另外抓一次记录窗筛选态与设置窗「外观与窗口」卡片。
const path = require('path')
const fs = require('fs')

const PW = 'C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core'
const { chromium } = require(PW)

const OUT = process.argv[2] || path.join(process.cwd(), '.tmp-shots')

// 与 Rust open_aux_impl / tauri.conf.json 保持一致
const WINDOWS = [
  { route: 'main', file: 'w-main', w: 360, h: 500 },
  { route: 'history', file: 'w-history', w: 840, h: 640 },
  { route: 'settings', file: 'w-settings', w: 640, h: 700 },
]

async function main() {
  fs.mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const logs = []

  for (const win of WINDOWS) {
    const page = await browser.newPage({
      viewport: { width: win.w, height: win.h },
      deviceScaleFactor: 2,
    })
    page.on('console', (m) => logs.push(`[${win.route}][${m.type()}] ${m.text()}`))
    page.on('pageerror', (e) => logs.push(`[${win.route}][pageerror] ${e.message}`))

    await page.goto(`http://127.0.0.1:5173/#/${win.route}`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(1600)
    await page.screenshot({ path: path.join(OUT, `${win.file}.png`) })

    const m = await page.evaluate(() => {
      const body = document.querySelector('.body')
      const card = document.querySelector('.rec-card')
      const head = document.querySelector('.rec-head')
      const row = document.querySelector('.rec-main')
      const cols = (el) =>
        el ? getComputedStyle(el).gridTemplateColumns.split(' ').map((v) => Math.round(parseFloat(v))) : null
      return {
        route: location.hash,
        bodyScrollH: body ? body.scrollHeight : 0,
        bodyClientH: body ? body.clientHeight : 0,
        hOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        recCardW: card ? Math.round(card.getBoundingClientRect().width) : null,
        headCols: cols(head),
        rowCols: cols(row),
        rows: document.querySelectorAll('.rec-row').length,
      }
    })
    console.log(win.route, JSON.stringify(m))

    // 记录窗：切一次筛选，看下拉胶囊在深色下的观感
    if (win.route === 'history') {
      const sel = await page.$('select.fchip')
      if (sel) {
        await sel.selectOption({ index: 1 })
        await page.waitForTimeout(400)
        await page.screenshot({ path: path.join(OUT, `${win.file}-filtered.png`) })
      }
    }
    // 设置窗：滚到「外观与窗口」卡片
    if (win.route === 'settings') {
      await page.evaluate(() => {
        const h3 = Array.from(document.querySelectorAll('.card h3')).find((n) => n.textContent.includes('外观与窗口'))
        if (h3) h3.scrollIntoView({ block: 'start' })
      })
      await page.waitForTimeout(400)
      await page.screenshot({ path: path.join(OUT, `${win.file}-window-card.png`) })
    }
    await page.close()
  }

  console.log('--- console ---')
  console.log(logs.slice(0, 20).join('\n'))
  await browser.close()
}

main().catch((e) => {
  console.error(e)
  process.exit(1)
})
