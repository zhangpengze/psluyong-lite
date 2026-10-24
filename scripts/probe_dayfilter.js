/* 验证"点日期分组标题 = 只看这天"与自定义日期框 */
const PW = 'C:/Users/Administrator/.workbuddy/binaries/node/workspace/node_modules/playwright-core'
const { chromium } = require(PW)
;(async () => {
  const b = await chromium.launch({ channel: 'msedge', headless: true })
  const p = await b.newPage({ viewport: { width: 840, height: 640 } })
  const reqs = []
  p.on('request', (r) => {
    if (r.url().includes('/history?')) reqs.push(decodeURIComponent(r.url().split('/history?')[1]))
  })
  await p.goto('http://127.0.0.1:5173/#/history', { waitUntil: 'networkidle' })
  await p.waitForTimeout(1200)

  const state = () =>
    p.evaluate(() => ({
      summary: document.querySelector('.range-more')?.textContent?.trim(),
      chipsActive: [...document.querySelectorAll('.range-chip')].filter((e) => e.classList.contains('active')).map((e) => e.textContent.trim()),
      picked: [...document.querySelectorAll('.date-group.picked')].map((e) => e.textContent.replace(/\s+/g, ' ').trim()),
      groups: [...document.querySelectorAll('.date-group')].length,
      rows: [...document.querySelectorAll('.rec-row')].length,
      dates: [...document.querySelectorAll('.dchip')].map((e) => e.value),
      datePickActive: !!document.querySelector('.date-pick.active'),
    }))

  console.log('① 初始：', JSON.stringify(await state()))

  // 点第二个分组标题（昨天）
  const groups = await p.$$('.date-group')
  console.log('   分组数：', groups.length)
  const target = await groups[1].evaluate((e) => e.textContent.replace(/\s+/g, ' ').trim())
  await groups[1].click()
  await p.waitForTimeout(1200)
  console.log('② 点了「' + target + '」→', JSON.stringify(await state()))
  await p.screenshot({ path: '.shots/w-history-day.png' })

  // 再点一次 = 取消
  await p.click('.date-group.picked')
  await p.waitForTimeout(1000)
  console.log('③ 再点一次取消 →', JSON.stringify(await state()))

  // 手动填日期框
  await p.fill('.dchip >> nth=0', '2026-10-01')
  await p.waitForTimeout(900)
  await p.fill('.dchip >> nth=1', '2026-10-12')
  await p.waitForTimeout(1100)
  console.log('④ 日期框 10-01~10-12 →', JSON.stringify(await state()))
  await p.screenshot({ path: '.shots/w-history-range.png' })

  console.log('\n实际请求：')
  for (const r of reqs) console.log('  ', r)
  await b.close()
})()
