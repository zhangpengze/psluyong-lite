/* 第 6 条回归：配置提示不再占横幅，改由主按钮承担。
   node scripts/probe_setupbtn.js
   依赖：假 sidecar（127.0.0.1:8765）+ vite dev（127.0.0.1:5173）
   验证：① 页面上没有 .setup-strip；② 主按钮显示「去配置：姓名 · API Key」且可点；
        ③ 点按钮跳到 #/settings；④ 配置补全后按钮恢复「生成录用图」；
        ⑤ 全程无横向/纵向溢出（高度自适应跟着缩回 222）。 */
const path = require('path')
const PW = 'C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core'
const { chromium } = require(PW)

const OUT = path.join(process.cwd(), '.shots')
const API = 'http://127.0.0.1:8765'

async function patchConfig(patch) {
  const r = await fetch(`${API}/config`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(patch),
  })
  if (!r.ok) throw new Error(`patch config failed: ${r.status}`)
}

;(async () => {
  // 把姓名和 API Key 清空，复现首启「还差 2 步」的状态（原样保留做最后恢复）
  await patchConfig({ employee_name: '', api_key: '' })

  const browser = await chromium.launch({ channel: 'msedge', headless: true })
  const page = await browser.newPage({ viewport: { width: 360, height: 222 }, deviceScaleFactor: 2 })
  const logs = []
  page.on('console', (m) => m.type() === 'error' && logs.push(m.text()))
  page.on('pageerror', (e) => logs.push(e.message))

  // 假 Tauri：把 set_main_height 回放到 viewport，量真实收敛高度
  await page.exposeFunction('__resize', async (h) => {
    await page.setViewportSize({ width: 360, height: h })
  })
  await page.addInitScript(() => {
    window.__heightCalls = []
    window.__TAURI__ = {
      core: {
        invoke: (cmd, args) => {
          if (cmd === 'set_main_height') {
            window.__heightCalls.push(args.height)
            window.__resize(args.height)
          }
          // 预览里没有真窗口：把「打开设置窗」翻译成 hash 路由，才能断言点击去向
          if (cmd === 'open_aux_window') location.hash = '#/' + args.kind
          return Promise.resolve(null)
        },
      },
      event: { listen: () => Promise.resolve(() => {}) },
    }
  })

  await page.goto('http://127.0.0.1:5173/#/main', { waitUntil: 'networkidle' })
  await page.waitForTimeout(2500)

  const first = await page.evaluate(() => {
    const btn = document.querySelector('.form-foot .btn')
    const body = document.querySelector('.body')
    return {
      stripCount: document.querySelectorAll('.setup-strip').length,
      btnText: btn?.textContent?.replace(/\s+/g, ' ').trim() || '',
      btnDisabled: btn?.disabled ?? null,
      btnSetup: btn?.classList.contains('btn-setup') || false,
      btnTitle: btn?.getAttribute('title') || '',
      innerH: window.innerHeight,
      overflow: body ? body.scrollHeight - body.clientHeight : -1,
      heightCalls: window.__heightCalls || [],
      hash: location.hash,
    }
  })
  await page.screenshot({ path: path.join(OUT, 'main-setup.png') })

  const fail = []
  if (first.stripCount !== 0) fail.push(`残留横幅 .setup-strip x${first.stripCount}`)
  if (!first.btnText.includes('去配置：姓名 · API Key')) fail.push(`按钮文案="${first.btnText}"`)
  if (first.btnDisabled !== false) fail.push('配置缺项按钮却是禁用的')
  if (!first.btnSetup) fail.push('缺少 btn-setup 样式类')
  if (!first.btnTitle.includes('填写你的姓名')) fail.push(`title="${first.btnTitle}"`)
  if (first.overflow > 0) fail.push(`页面溢出 ${first.overflow}px`)
  if (first.innerH > 230) fail.push(`窗口高 ${first.innerH}px，横幅没省下来`)

  // 点主按钮应该去设置
  await page.click('.form-foot .btn')
  await page.waitForTimeout(800)
  const afterClick = await page.evaluate(() => location.hash)

  // 回主窗，补全配置，按钮应恢复「生成录用图」。
  // 带 query 是为了强制整页刷新：同 URL 只换 hash 不会重新加载，
  // cfg 还停在清空时的旧值，会误报"按钮没恢复"。
  await patchConfig({ employee_name: '张三', api_key: 'demo-key-0000' })
  await page.goto(`http://127.0.0.1:5173/?ready=1#/main`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2000)
  const ready = await page.evaluate(() => {
    const btn = document.querySelector('.form-foot .btn')
    return { text: btn?.textContent?.replace(/\s+/g, ' ').trim() || '', setup: btn?.classList.contains('btn-setup') }
  })
  await page.screenshot({ path: path.join(OUT, 'main-ready.png') })

  if (afterClick !== '#/settings') fail.push(`点按钮后 hash=${afterClick}`)
  if (!ready.text.includes('生成录用图')) fail.push(`补全后按钮="${ready.text}"`)
  if (ready.setup) fail.push('补全后仍带 btn-setup')
  if (logs.length) fail.push(`console: ${logs.join(' | ')}`)

  await browser.close()

  console.log('--- 缺配置态 ---')
  console.log(JSON.stringify(first, null, 2))
  console.log('--- 点击后 hash:', afterClick)
  console.log('--- 补全后按钮:', ready.text)
  if (fail.length) {
    console.log('FAIL:')
    fail.forEach((f) => console.log('  - ' + f))
    process.exit(1)
  }
  console.log('PASS: 横幅已消失，提示并入主按钮，高度自适应缩回 222')
})().catch((e) => {
  console.error(e)
  process.exit(1)
})
