<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, type ClientConfig, type PsInfo, type TemplateItem } from './api/sidecar'
import { useConfirm } from './composables/useConfirm'
import { useToast } from './composables/useToast'
import {
  broadcastEvent,
  checkAutoDock,
  forceQuit,
  getWindowRect,
  hasTauri,
  hideMainWindow,
  listenAppEvent,
  listenCloseWhileBusy,
  listenDockChanged,
  listenWindowMoved,
  dockMainNearest,
  openWindow,
  restoreWindowRect,
  setCloseToTray,
  setGenerating,
  setMainAlwaysOnTop,
  setMainDock,
  setMainHeight,
  setTrayTooltip,
  type DockSide,
} from './api/windowCtl'
import RecordForm from './components/RecordForm.vue'
import HistoryPanel from './components/HistoryPanel.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import ToastHost from './components/ToastHost.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'
import { localDateKey } from './utils/history'

const { confirm } = useConfirm()
const { toast } = useToast()

// ==================== 路由：三个窗口，各看各的页面 ====================
// 主窗 #/main、记录窗 #/history、设置窗 #/settings。
// hash 路由而不是多入口 HTML：Rust 只建窗口，页面由这里分发，
// 共享的状态（配置/模板/主题/toast）也集中在这一层。
type Route = 'main' | 'history' | 'settings'
const ROUTES: Route[] = ['main', 'history', 'settings']

function routeFromHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, '')
  return (ROUTES as string[]).includes(raw) ? (raw as Route) : 'main'
}

const route = ref<Route>(routeFromHash())

const cfg = ref<ClientConfig | null>(null)
const templates = ref<TemplateItem[]>([])
const partners = ref<string[]>([])
const serverOk = ref(false)
/** 本机 Photoshop 的自动识别结果（sidecar /status 带下来） */
const ps = ref<PsInfo | null>(null)
const comAvailable = ref(false)
const pending = ref(0)
const deadCount = ref(0)
const booting = ref(true)
const loadError = ref('')
// 生成成功后让记录面板重新拉一次（同窗口内），跨窗口走 record-added 事件
const historyVersion = ref(0)
// 记录窗今日生成数（HistoryPanel 回传，用于托盘气泡）
const todayCount = ref(0)

const theme = ref<'dark' | 'light'>('dark')

// ==================== 生成中状态（关窗拦截 + 贴边不缩回用）====================
// formBusy：录用页实时上报；serverBusy：sidecar 权威值（切窗口后仍然准确）
const formBusy = ref(false)
const serverBusy = ref(false)
const generating = computed(() => formBusy.value || serverBusy.value)
watch(generating, (v) => {
  if (hasTauri()) setGenerating(v).catch(() => {})
})

/** 配置完成度：姓名 / API Key / 服务器地址 三项。
 *  short 是按钮上用的短词（按钮只有 340px 宽，塞不下整句）；
 *  label 是悬停提示里的完整说法。 */
const setupItems = computed(() => [
  { label: '填写你的姓名', short: '姓名', done: !!cfg.value?.employee_name },
  { label: '填写服务器地址', short: '服务器地址', done: !!cfg.value?.server_url },
  { label: '填写 API Key（向主管索取）', short: 'API Key', done: !!cfg.value?.api_key },
])
/** 只列还没做的项，已完成的不占地方；短词印在主按钮上，完整说法挂按钮 title */
const missingItems = computed(() => setupItems.value.filter((i) => !i.done))

const deadHint = computed(
  () =>
    `有 ${deadCount.value} 条记录多次补传都没成功，已移到本地 dead 目录留档。` +
    '确认服务器恢复正常后点「重试死信」再试一次。',
)

/**
 * @param quiet 静默失败：轮询时不弹大错误条（离线有状态点表达），
 *              只有启动阶段才需要把失败摆到台面上
 */
async function refreshStatus(quiet = false): Promise<boolean> {
  try {
    const s = await api.status()
    cfg.value = s.config
    serverOk.value = s.server_reachable
    comAvailable.value = s.com_available
    ps.value = s.ps ?? null
    pending.value = s.pending
    deadCount.value = s.dead ?? 0
    serverBusy.value = !!s.busy
    loadError.value = ''
    // 主题以服务端配置为准（换机器/重装不丢偏好）
    const next = s.config.theme === 'light' ? 'light' : 'dark'
    if (next !== theme.value) applyTheme(next)
    return true
  } catch (e) {
    if (!quiet) loadError.value = e instanceof Error ? e.message : '本地服务连接失败'
    return false
  }
}

/**
 * sidecar 是 Tauri 启动的子进程，比窗口晚 1~3 秒就绪是常态。
 * 只试一次就报「请重启软件」，员工每次开机都要看一次红条（真实踩坑）。
 */
async function waitForSidecar(): Promise<boolean> {
  const delays = [0, 400, 800, 1200, 1600, 2000, 2500, 3000]
  for (const d of delays) {
    if (d) await new Promise((r) => setTimeout(r, d))
    if (await refreshStatus(true)) return true
  }
  loadError.value = '本地服务连接失败'
  return false
}

async function refreshTemplates() {
  try {
    const res = await api.templates()
    if (res.ok) templates.value = res.items
    else loadError.value = res.error || '获取模板失败'
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '获取模板失败'
  }
}

async function refreshDicts() {
  try {
    const res = await api.dicts()
    partners.value = res.grouped?.partner ?? []
  } catch {
    partners.value = []
  }
}

async function flushQueue() {
  try {
    const res = await api.flushQueue()
    pending.value = res.remaining
    deadCount.value = res.dead ?? deadCount.value
    if (res.sent > 0) refreshTemplates()
  } catch {
    /* 补传失败不打扰员工 */
  }
}

/** 死信是多次补传仍失败的记录，只有人确认服务端正常了才该重试 */
async function retryDead() {
  if (!(await confirm({ title: '重试死信', message: `把 ${deadCount.value} 条死信重新排入补传队列？`, confirmText: '重试' }))) return
  try {
    const res = await api.retryDead()
    pending.value = res.remaining
    deadCount.value = res.dead
    if (res.sent > 0) {
      refreshTemplates()
      toast(`已补传 ${res.sent} 条死信`, 'success')
    }
    if (res.restored > 0 && res.sent === 0) {
      toast(`已重新排队 ${res.restored} 条，但本次仍未成功，请检查服务器`, 'warning')
    }
  } catch {
    toast('重试失败，请稍后再试', 'error')
  }
}

function onConfigSaved(next: ClientConfig) {
  const prev = cfg.value
  cfg.value = next
  applyTheme(next.theme === 'light' ? 'light' : 'dark')
  refreshTemplates()
  refreshDicts()
  refreshStatus()
  // 窗口类偏好只有主窗能应用：命令在 Rust 里固定操作 main 标签
  if (hasTauri()) {
    if (!prev || prev.always_on_top !== next.always_on_top) {
      setMainAlwaysOnTop(next.always_on_top).catch(() => {})
      pinned.value = next.always_on_top
    }
    if (!prev || prev.dock_side !== next.dock_side) {
      setMainDock(next.dock_side as DockSide).catch(() => {})
    }
    if (!prev || prev.close_to_tray !== next.close_to_tray) {
      setCloseToTray(next.close_to_tray).catch(() => {})
    }
  }
  // 其他窗口（记录窗）同步主题与状态
  broadcastEvent('config-changed', { theme: next.theme }).catch(() => {})
}

function onExported() {
  historyVersion.value += 1
  refreshStatus()
  refreshToday()
  // 记录窗（可能开着）收到后自动刷新，员工不用手动点
  broadcastEvent('record-added', {}).catch(() => {})
}

function onHistoryStats(stats: { total: number; today: number }) {
  todayCount.value = stats.today
}

/**
 * 今日生成数：托盘气泡要显示，但记录窗不常开。
 * 只靠记录窗回传的话，主窗运行时气泡永远是「今日 0」，所以主窗自己也拉一次。
 */
async function refreshToday() {
  try {
    const res = await api.history('7')
    if (!res.ok) return
    const key = localDateKey(new Date())
    todayCount.value = (res.items ?? []).filter((it) => it.work_date === key).length
  } catch {
    /* 拉不到就保留上一次的数字，不值得打扰员工 */
  }
}

// ==================== 主题 ====================
function applyTheme(t: 'dark' | 'light') {
  theme.value = t
  document.documentElement.dataset.theme = t
}

/**
 * 主窗头部一键切主题。
 *
 * 设置里原来也有「主题」——同一个开关两处都能改，员工每次都得想一下该去哪儿，
 * 于是撤了。按钮上显示的是**当前**主题的图标（深色=月亮、浅色=太阳），
 * title 说明点下去会变成什么，不用猜。
 */
async function toggleTheme() {
  const next = theme.value === 'light' ? 'dark' : 'light'
  applyTheme(next)
  try {
    await api.saveConfig({ theme: next })
    // 记录窗/设置窗可能开着：它们各自 refreshStatus 时会读到新配置，这里只负责通知
    broadcastEvent('config-changed', { theme: next }).catch(() => {})
  } catch {
    /* 存不上面至少本次生效，下次启动回到旧主题，不值得弹错 */
  }
}

// ==================== 窗口：置顶 / 贴边 / 位置记忆 / 多窗 ====================
// 浏览器预览里 hasTauri() 为 false，按钮隐藏、方法 no-op
const hasWindowCtl = ref(hasTauri())
const pinned = ref(false)
const docked = ref(false)
let rectTimer: number | undefined
let moveTimer: number | undefined
/** 启动期的程序化位移（恢复位置/贴边）不触发自动贴边，就绪后再武装 */
let autoDockArmed = false

async function togglePin() {
  const next = !pinned.value
  try {
    await setMainAlwaysOnTop(next)
    pinned.value = next
    await api.saveConfig({ always_on_top: next })
  } catch {
    /* 置顶失败极少见，不打扰员工 */
  }
}

/**
 * 主窗上的贴边按钮：开 / 关。
 * 方向不在这里选，也不从配置里读——贴哪边由 Rust 按"窗口离哪条屏缘近"决定
 * （和 QQ 自动隐藏一致）。dock-changed 回来后只负责把"上次贴到哪边"落盘，
 * 设置里已经没有这一项了，它纯粹是下次启动还原用的状态。
 */
async function toggleDock() {
  try {
    if (docked.value) await setMainDock('')
    else await dockMainNearest()
  } catch {
    /* 贴边失败保持原状 */
  }
}

/** 打开记录窗 / 设置窗：带上各自记住的位置，没有就居中 */
function openAux(kind: 'history' | 'settings') {
  if (!hasTauri()) {
    // 浏览器预览：没有多窗口，用 hash 路由原地看页面
    window.location.hash = `#/${kind}`
    return
  }
  const x = kind === 'history' ? cfg.value?.history_win_x ?? -1 : cfg.value?.settings_win_x ?? -1
  const y = kind === 'history' ? cfg.value?.history_win_y ?? -1 : cfg.value?.settings_win_y ?? -1
  openWindow(kind, x, y).catch(() => {})
}

/** 定期把窗口位置存进配置，下次启动恢复 */
async function rememberWindowRect() {
  if (!hasTauri() || docked.value) return
  const rect = await getWindowRect()
  if (!rect) return
  const [x, y] = rect
  if (route.value === 'history') {
    if (x !== cfg.value?.history_win_x || y !== cfg.value?.history_win_y) {
      await api.saveConfig({ history_win_x: x, history_win_y: y })
    }
  } else if (route.value === 'settings') {
    if (x !== cfg.value?.settings_win_x || y !== cfg.value?.settings_win_y) {
      await api.saveConfig({ settings_win_x: x, settings_win_y: y })
    }
  } else if (x !== cfg.value?.win_x || y !== cfg.value?.win_y) {
    await api.saveConfig({ win_x: x, win_y: y })
  }
}

async function applyWindowPrefs(c: ClientConfig) {
  if (!hasTauri()) return
  if (c.win_x >= 0) {
    // 先恢复上次的窗口位置；若贴边，dock 命令会再接管位置
    await restoreWindowRect(c.win_x, c.win_y).catch(() => {})
  }
  if (c.always_on_top) {
    await setMainAlwaysOnTop(true).catch(() => {})
    pinned.value = true
  }
  if (c.close_to_tray) await setCloseToTray(true).catch(() => {})
  if (c.dock_side === 'left' || c.dock_side === 'right' || c.dock_side === 'top') {
    await setMainDock(c.dock_side as DockSide).catch(() => {})
    docked.value = true
  }
}

// ==================== 主窗高度自适应 ====================
// 迷你主窗只有「模板+合作伙伴 / 作者 / 标题 / 生成按钮」四行。高度写死必然
// 留死白：380 高时下方空出 140px（占 37%，纯浪费），压到 260 又装不下
// 生成结果条 + 注意事项。所以高度不写死，让内容决定：
//   内容变了 → 量一次自然高度 → 让 Rust 改窗口高度。
// 员工手动拖过高度（与我们所设值差 > 12px）就把控制权交还给他，不再抢。
const MAIN_H_MIN = 180
const MAIN_H_MAX = 560
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
  if (!hasTauri() || route.value !== 'main' || !autoFit) return
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
 * 只在主窗生效——记录窗/设置窗是固定尺寸、按需滚动，不该跟着内容缩。
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

// ==================== 轮询、快捷键、跨窗口同步 ====================
let statusTimer: number | undefined

/** Esc 隐藏主窗：迷你工具的快速唤出/隐藏，像输入法一样 */
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && route.value === 'main' && hasTauri()) {
    hideMainWindow().catch(() => {})
  }
}

/** 托盘气泡：在线状态 + 今日进度 + 待补传，不开窗也知道全局 */
watch([serverOk, pending, todayCount], () => {
  if (!hasTauri()) return
  const state = serverOk.value ? '在线' : '离线'
  const text = `期刊录用助手 · ${state} · 今日 ${todayCount.value} · 待补传 ${pending.value}`
  setTrayTooltip(text).catch(() => {})
})

let started = false

/** 本地服务就绪后才做的事：模板/名单、补传、窗口偏好、轮询、事件 */
async function startAfterReady() {
  if (started) return
  started = true

  await Promise.all([refreshTemplates(), refreshDicts()])
  flushQueue()
  refreshToday()

  // 窗口三件套：恢复上次的位置/置顶/贴边
  if (cfg.value) applyWindowPrefs(cfg.value)
  listenDockChanged((side) => {
    docked.value = side !== null
    // 只记住"贴在哪一边"：设置里已经不提供方向选择，这项纯粹是下次启动还原用
    api.saveConfig({ dock_side: side ?? '' }).catch(() => {})
  })
  rectTimer = window.setInterval(rememberWindowRect, 10_000)

  // 生成中关窗拦截：Rust 拦下并通知到这里，确认后才真的退出
  listenCloseWhileBusy(async () => {
    const ok = await confirm({
      title: '正在生成中',
      message: '录用图还在生成（或等待 PS 响应）。现在关闭会中断本次生成，Photoshop 里可能残留已打开的模板。',
      danger: true,
      confirmText: '仍然退出',
      cancelText: '继续等待',
    })
    if (ok) await forceQuit().catch(() => {})
  })

  // 跨窗口同步：设置窗保存后这里刷新主题；主窗生成完成后记录窗刷新
  listenAppEvent('config-changed', () => {
    refreshStatus(true)
  })
  listenAppEvent('record-added', () => {
    historyVersion.value += 1
    refreshStatus(true)
  })

  // 拖到边缘自动贴边：监听窗口移动，停止 300ms 后让 Rust 判边缘距离。
  // 启动期的程序化位移（恢复位置/贴边）不算数，就绪 1s 后才武装。
  if (cfg.value?.auto_dock !== false) {
    listenWindowMoved(() => {
      if (!autoDockArmed) return
      if (moveTimer) window.clearTimeout(moveTimer)
      moveTimer = window.setTimeout(() => {
        checkAutoDock().catch(() => {})
      }, 300)
    })
    window.setTimeout(() => (autoDockArmed = true), 1000)
  }

  // 状态轮询：服务器恢复后自动补传离线队列，员工不用记着手动点
  statusTimer = window.setInterval(async () => {
    const wasOffline = !serverOk.value
    await refreshStatus(true)
    if (wasOffline && serverOk.value && pending.value > 0) {
      await flushQueue()
      if (pending.value === 0) toast('服务器已恢复，离线记录已自动补传', 'success')
    }
    // 别的机器/别的窗口可能又生成了，气泡数字跟着走
    refreshToday()
  }, 60_000)
}

/** 错误卡片上的「重试连接」：重新等 sidecar，成功后再走一遍启动流程 */
async function retryBoot() {
  booting.value = true
  loadError.value = ''
  const ready = await waitForSidecar()
  booting.value = false
  if (ready) await startAfterReady()
}

/** 主窗内容就位后再开始自适应：启动期只有「正在启动…」，量了也没意义 */
watch([booting, route], async () => {
  await nextTick()
  if (booting.value || route.value !== 'main') return
  watchMainContent()
  scheduleFit()
})

onMounted(async () => {
  // hash 驱动路由：三个窗口打开时各自带上 #/main #/history #/settings
  window.addEventListener('hashchange', () => {
    route.value = routeFromHash()
  })
  window.addEventListener('keydown', onKeydown)
  window.addEventListener('resize', onWinResize)

  booting.value = true
  const ready = await waitForSidecar()
  booting.value = false
  if (!ready) return // 停在错误卡片上，等员工点「重试连接」

  await startAfterReady()
})

onUnmounted(() => {
  if (rectTimer) window.clearInterval(rectTimer)
  if (statusTimer) window.clearInterval(statusTimer)
  if (moveTimer) window.clearTimeout(moveTimer)
  if (fitRaf) cancelAnimationFrame(fitRaf)
  contentRO?.disconnect()
  contentMO?.disconnect()
  window.removeEventListener('keydown', onKeydown)
  window.removeEventListener('resize', onWinResize)
})
</script>

<template>
  <div class="wallpaper" aria-hidden="true" />

  <div class="app">
    <!-- 主窗头部：logo + 记录/设置入口 + 窗口控件 + 服务器状态（单行） -->
    <header v-if="route === 'main'" class="header">
      <div class="h-logo">录</div>
      <div class="h-brand">期刊录用助手</div>

      <div class="h-actions">
        <button class="icon-btn" title="记录（独立窗口）" aria-label="打开记录窗口" @click="openAux('history')">
          <svg viewBox="0 0 16 16" width="13" height="13">
            <circle cx="8" cy="8" r="5.8" fill="none" stroke="currentColor" stroke-width="1.3" />
            <path d="M8 4.8V8l2.3 1.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
          </svg>
        </button>
        <button class="icon-btn" title="设置（独立窗口）" aria-label="打开设置窗口" @click="openAux('settings')">
          <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="8" cy="8" r="2.1" />
            <path d="M8 1.9v1.8M8 12.3v1.8M1.9 8h1.8M12.3 8h1.8M3.6 3.6l1.3 1.3M11.1 11.1l1.3 1.3M12.4 3.6l-1.3 1.3M4.9 11.1l-1.3 1.3" />
          </svg>
        </button>
        <template v-if="hasWindowCtl">
          <button class="icon-btn" :class="{ active: pinned }" title="置顶窗口" aria-label="置顶窗口" @click="togglePin">
            <svg viewBox="0 0 16 16" width="13" height="13">
              <path d="M9.5 1.8l4.7 4.7-2.1.6-1.2 3.2-2.2-2.2-3.4 3.4 3.4-3.4-2.2-2.2 3.2-1.2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
            </svg>
          </button>
          <button class="icon-btn" :class="{ active: docked }" title="贴边隐藏" aria-label="贴边隐藏" @click="toggleDock">
            <svg viewBox="0 0 16 16" width="13" height="13">
              <rect x="2.2" y="2.6" width="11.6" height="10.8" rx="2" fill="none" stroke="currentColor" stroke-width="1.3" />
              <path d="M2.2 5.4h3.2v5.2H2.2" fill="none" stroke="currentColor" stroke-width="1.3" />
            </svg>
          </button>
        </template>
        <!-- 主题一键切换：设置里那份已撤，同一个开关不该有两个入口。
             用"半黑半白的圆"而不是太阳/月亮：设置图标本身就是圆+射线，
             在 360px 的头部里再来一个太阳，员工分不清谁是谁。
             当前是深是浅看窗口就知道，title 说明点下去会变成什么。 -->
        <button
          class="icon-btn"
          :title="theme === 'light' ? '切换到深色主题' : '切换到浅色主题'"
          aria-label="切换主题"
          @click="toggleTheme"
        >
          <svg viewBox="0 0 16 16" width="13" height="13">
            <circle cx="8" cy="8" r="5.9" fill="none" stroke="currentColor" stroke-width="1.3" />
            <path d="M8 2.1a5.9 5.9 0 0 0 0 11.8z" fill="currentColor" />
          </svg>
        </button>
        <span class="dot" :class="serverOk ? 'on' : 'off'" :title="serverOk ? '服务器正常' : '服务器离线，记录会存在本地'">
          {{ serverOk ? '' : '离线' }}
        </span>
      </div>
    </header>

    <main ref="bodyRef" class="body" :class="{ bare: route !== 'main' }">
      <div v-if="booting" class="empty">
        <span class="e-ico">
          <svg viewBox="0 0 16 16" width="18" height="18">
            <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-dasharray="28 12" />
          </svg>
        </span>
        正在启动…
      </div>

      <template v-else>
        <div v-if="loadError" class="result fail">
          <div class="head"><span class="ico">!</span>{{ loadError }}</div>
          <div>客户端可能还在启动本地服务，点下面按钮再试一次；仍不行请重启软件。</div>
          <button class="btn ghost sm" style="margin-top: 9px" @click="retryBoot">重试连接</button>
        </div>

        <!-- 配置没配完时不再单独占一条横幅：那句话搬到主按钮上
             （"还差 2 步：姓名 · API Key"，点它直接去设置）。
             首启引导本来就该长在"现在该做什么"上，而不是常驻一块地方。 -->

        <div v-if="!comAvailable && route === 'main'" class="result warn">
          <div class="head"><span class="ico">!</span>本机缺少 PS 自动化组件</div>
          <div>未检测到 pywin32，无法调用 Photoshop。请安装依赖后重启客户端。</div>
        </div>

        <!-- 装没装 PS 是运行时才知道的事：识别不到就在这儿说一声，
             别等员工点完生成才弹一个看不懂的 COM 错误。
             COM 组件缺失（pywin32）优先报，那条更根本。 -->
        <div v-else-if="ps && !ps.found && route === 'main'" class="result warn">
          <div class="head"><span class="ico">!</span>未识别到本机安装的 Photoshop</div>
          <div>
            自动识别没找到 Photoshop（任意版本、任意盘符都会找）。请先安装，
            或到设置里手动指定路径。
          </div>
          <button class="btn ghost sm" style="margin-top: 9px" @click="openAux('settings')">去设置</button>
        </div>

        <RecordForm
          v-if="route === 'main'"
          :config="cfg"
          :templates="templates"
          :partners="partners"
          :ready="!!cfg?.employee_name && !!cfg?.api_key"
          :missing="missingItems.map((i) => i.short)"
          :missing-detail="missingItems.map((i) => i.label).join('、')"
          @exported="onExported"
          @busy-change="formBusy = $event"
          @need-settings="openAux('settings')"
        />

        <HistoryPanel
          v-else-if="route === 'history'"
          :version="historyVersion"
          :config="cfg"
          :templates="templates"
          :partners="partners"
          @stats="onHistoryStats"
        />

        <SettingsPanel
          v-else
          :config="cfg"
          :server-ok="serverOk"
          :ps="ps"
          :templates="templates"
          @saved="onConfigSaved"
          @templates-changed="refreshTemplates"
        />
      </template>
    </main>

    <!-- 主窗底栏：无事可报时整体隐藏（离线/待补传/死信才出现），姓名悬停可见 -->
    <footer
      v-if="route === 'main' && (pending > 0 || deadCount > 0 || !serverOk)"
      class="statusbar"
    >
      <span class="me" :title="cfg?.employee_name || ''">{{ cfg?.employee_name || '未设置姓名' }}</span>
      <span v-if="!serverOk">服务器离线</span>
      <span v-if="pending > 0">待补传 {{ pending }} 条</span>
      <span v-if="deadCount > 0" class="dead" :title="deadHint">死信 {{ deadCount }} 条</span>
      <div class="spacer" />
      <a v-if="pending > 0" class="link" @click="flushQueue">立即补传</a>
      <a v-if="deadCount > 0" class="dead-link" @click="retryDead">重试死信</a>
    </footer>
  </div>

  <ToastHost />
  <ConfirmDialog />
</template>
