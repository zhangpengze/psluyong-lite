/* 主窗高度自适应的回归探针：用假的 __TAURI__ 跑「量 → 改窗 → 收敛」全链路，
   验证「不留白 / 不长出滚动条 / 不来回抖 / 不抢宽度」。
   node scripts/probe_autofit.js
   依赖：假 sidecar（127.0.0.1:8765）+ vite dev（127.0.0.1:5173）
   期望：①② 均 innerH=222 且无 set_main_height 调用（配置高度已经刚好）；
        ③ 加结果条 → 一次调用长到 259，④ 移除 → 一次调用缩回 222。 */
const PW = 'C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core'
const { chromium } = require(PW)

;(async () => {
  const b = await chromium.launch({ channel: 'msedge', headless: true })
  const p = await b.newPage({ viewport: { width: 360, height: 222 } })
  await p.exposeFunction('__resize', async (h) => {
    await p.setViewportSize({ width: 360, height: h })
  })
  await p.addInitScript(() => {
    window.__calls = []
    window.__TAURI__ = {
      core: {
        invoke: (cmd, args) => {
          window.__calls.push({ cmd, args })
          if (cmd === 'set_main_height') window.__resize(args.height)
          return Promise.resolve(null)
        },
      },
      event: { listen: () => Promise.resolve(() => {}) },
    }
  })
  await p.goto('http://127.0.0.1:5173/#/main', { waitUntil: 'networkidle' })
  await p.waitForTimeout(2500)

  const state = () =>
    p.evaluate(() => {
      const bd = document.querySelector('.body')
      const btn = document.querySelector('.btn')
      return {
        innerH: innerHeight,
        scrollH: bd.scrollHeight,
        clientH: bd.clientHeight,
        overflow: bd.scrollHeight > bd.clientHeight,
        bottomGap: Math.round(innerHeight - btn.getBoundingClientRect().bottom),
        heights: window.__calls.filter((c) => c.cmd === 'set_main_height').map((c) => c.args.height),
        // 一次也不能抢宽度
        otherCalls: window.__calls.filter((c) => c.cmd !== 'set_main_height').map((c) => c.cmd),
      }
    })

  console.log('① 启动收敛：', JSON.stringify(await state(), null, 0))
  await p.waitForTimeout(1200)
  console.log('② 静置 1.2s 后：', JSON.stringify(await state(), null, 0))

  await p.evaluate(() => {
    const foot = document.querySelector('.form-foot')
    const rbar = document.createElement('div')
    rbar.className = 'rbar fail'
    rbar.id = 'test-rbar'
    rbar.innerHTML = '<span class="rb-ico">!</span><span class="rb-msg">Photoshop 未响应（测试）</span>'
    foot.insertBefore(rbar, foot.firstChild)
  })
  await p.waitForTimeout(700)
  console.log('③ 出现结果条：', JSON.stringify(await state(), null, 0))

  await p.evaluate(() => document.getElementById('test-rbar')?.remove())
  await p.waitForTimeout(700)
  console.log('④ 结果条消失：', JSON.stringify(await state(), null, 0))

  await b.close()
})()
