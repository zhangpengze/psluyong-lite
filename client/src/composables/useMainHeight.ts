/**
 * 主窗高度自适应（composable）
 *
 * 迷你主窗只有「模板+合作伙伴 / 作者 / 标题 / 生成按钮」四行。高度写死必然
 * 留死白：380 高时下方空出 140px（占 37%，纯浪费），压到 260 又装不下
 * 生成结果条 + 注意事项。所以高度不写死，让内容决定：
 *   内容变了 → 量一次自然高度 → 让 Rust 改窗口高度。
 * 员工手动拖过高度（与我们所设值差 > 12px）就把控制权交还给他，不再抢。
 * 只在主窗生效——记录窗/设置窗是固定尺寸、按需滚动，不该跟着内容缩。
 */

import { nextTick, onMounted, onUnmounted, watch, type Ref } from 'vue'
import { hasTauri, setMainHeight } from '../api/windowCtl'
import type { Route } from './useHashRoute'

const MAIN_H_MIN = 180
const MAIN_H_MAX = 560

export function useMainHeight(opts: { route: Ref<Route>; booting: Ref<boolean> }) {
  let autoFit = true
  let appliedH = 0
  let fitRaf = 0
  let contentRO: ResizeObserver | undefined
  let contentMO: MutationObserver | undefined

  /**
   * 取一个块的内容高度。
   * `.stack` 是 flex:1 0 auto，会被容器撑满，它的 scrollHeight 只是"当前高度"；
   * 只有递归进它的孩子，量到的才是内容真实高度。
   */
  function intrinsicHeight(el: HTMLElement): number {
    const cs = getComputedStyle(el)
    if (el.classList.contains('stack')) {
      const gap = parseFloat(cs.rowGap || cs.gap) || 0
      let h = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
      let n = 0
      for (const kid of Array.from(el.children) as HTMLElement[]) {
        h += intrinsicHeight(kid) + (n++ ? gap : 0)
      }
      return h
    }
    return el.scrollHeight
  }

  /** 窗口应有的内高 = 头部 + 内容区各块（含内边距与间距）+ 底栏 */
  function naturalMainHeight(): number {
    const appEl = document.querySelector('.app') as HTMLElement | null
    const bodyEl = document.querySelector('.body') as HTMLElement | null
    if (!appEl || !bodyEl) return 0
    const cs = getComputedStyle(bodyEl)
    const gap = parseFloat(cs.rowGap || cs.gap) || 0
    let h = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom)
    let n = 0
    for (const kid of Array.from(bodyEl.children) as HTMLElement[]) {
      if (getComputedStyle(kid).position === 'absolute') continue
      h += intrinsicHeight(kid) + (n++ ? gap : 0)
    }
    for (const kid of Array.from(appEl.children) as HTMLElement[]) {
      if (kid === bodyEl || getComputedStyle(kid).position === 'absolute') continue
      h += kid.scrollHeight // 头部与底栏都是定高块，scrollHeight 即真实高度
    }
    return Math.ceil(h)
  }

  function fitMainHeight() {
    if (!hasTauri() || opts.route.value !== 'main' || !autoFit) return
    const bodyEl = document.querySelector('.body') as HTMLElement | null
    if (!bodyEl) return
    const natural = naturalMainHeight()
    if (!natural) return
    // 第一次先把当前高度认下来：手动拖拽检测要靠它比对
    if (!appliedH) appliedH = window.innerHeight
    // +1：亚像素取整后差一点点就会冒出滚动条，宁可多 1px
    let target = natural + 1
    // 已经溢出就别指望"自然高度"能一步算准：溢出的这一刻宽度被滚动条吃掉 7px，
    // 文字换行和自然高度都跟着变。直接按溢出差值补，补一次就到位。
    const overflowPx = bodyEl.scrollHeight - bodyEl.clientHeight
    if (overflowPx > 0) target = Math.max(target, window.innerHeight + overflowPx + 1)
    target = Math.min(MAIN_H_MAX, Math.max(MAIN_H_MIN, target))
    // 滞回：装不下立刻长高；装得下且富余 ≥ 6px 才缩。
    // 否则「滚动条出现→可用宽度减 7px→文字换行→自然高度变大→又缩回去」
    // 会来回抖，窗口底部一直抽搐。
    const needsGrow = overflowPx > 0
    const canShrink = window.innerHeight - target >= 6
    if (!needsGrow && !canShrink) return
    if (target === appliedH) return
    appliedH = target
    setMainHeight(target).catch(() => {})
  }

  function scheduleFit() {
    if (fitRaf) return
    fitRaf = requestAnimationFrame(() => {
      fitRaf = 0
      fitMainHeight()
    })
  }

  /**
   * 盯住内容：节点增删（结果条出现/收起、按钮文案切换）与自身高度变化都要重量。
   */
  function watchMainContent() {
    const bodyEl = document.querySelector('.body') as HTMLElement | null
    if (!bodyEl) return
    contentRO?.disconnect()
    contentRO = new ResizeObserver(() => scheduleFit())
    // 这些块不会被 flex 拉伸，高度变化就等于内容变化
    bodyEl.querySelectorAll('.form-body, .result').forEach((el) => contentRO!.observe(el))
    contentRO.observe(bodyEl)
    contentMO?.disconnect()
    contentMO = new MutationObserver(() => {
      watchMainContent()
      scheduleFit()
    })
    contentMO.observe(bodyEl, { childList: true, subtree: true })
  }

  /**
   * 员工自己拖过高度就不再多管：否则他拖大一点，下次生成完又被弹回去。
   * 容差 12px —— 我们自己的 setSize 落回来会有 1~2px 的取整差。
   */
  function onWinResize() {
    if (!appliedH) return
    if (Math.abs(window.innerHeight - appliedH) > 12) autoFit = false
  }

  /** 主窗内容就位后再开始自适应：启动期只有「正在启动…」，量了也没意义 */
  watch([opts.booting, opts.route], async () => {
    await nextTick()
    if (opts.booting.value || opts.route.value !== 'main') return
    watchMainContent()
    scheduleFit()
  })

  onMounted(() => window.addEventListener('resize', onWinResize))
  onUnmounted(() => {
    if (fitRaf) cancelAnimationFrame(fitRaf)
    contentRO?.disconnect()
    contentMO?.disconnect()
    window.removeEventListener('resize', onWinResize)
  })
}
