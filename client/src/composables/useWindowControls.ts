/**
 * 主窗窗口控制（composable）：置顶 / 贴边 / 位置记忆 / 打开子窗
 *
 * 浏览器预览里 hasTauri() 为 false，按钮隐藏、方法 no-op。
 * 贴哪边不从配置里读——由 Rust 按"窗口离哪条屏缘近"决定（和 QQ 自动隐藏一致），
 * dock-changed 回来后只把"上次贴到哪边"落盘，纯粹是下次启动还原用的状态。
 */

import { onUnmounted, ref, type Ref } from 'vue'
import { api, type ClientConfig } from '../api/sidecar'
import {
  checkAutoDock,
  dockMainNearest,
  getWindowRect,
  hasTauri,
  listenDockChanged,
  listenWindowMoved,
  openWindow,
  restoreWindowRect,
  setCloseToTray,
  setMainAlwaysOnTop,
  setMainDock,
  type DockSide,
} from '../api/windowCtl'
import type { Route } from './useHashRoute'

export interface WindowControlsOptions {
  /** 位置记忆 / 子窗坐标都要读配置 */
  cfg: Ref<ClientConfig | null>
  /** 记录窗 / 设置窗 / 主窗各自记住不同的坐标字段 */
  route: Ref<Route>
}

export function useWindowControls(opts: WindowControlsOptions) {
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
      await saveAndSync({ always_on_top: next })
    } catch {
      /* 置顶失败极少见，不打扰员工 */
    }
  }

  /**
   * 主窗上的贴边按钮：开 / 关。
   * 方向不在这里选——贴哪边由 Rust 按"窗口离哪条屏缘近"决定。
   * dock-changed 回来后只负责把"上次贴到哪边"落盘。
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
    const x = kind === 'history' ? opts.cfg.value?.history_win_x ?? -1 : opts.cfg.value?.settings_win_x ?? -1
    const y = kind === 'history' ? opts.cfg.value?.history_win_y ?? -1 : opts.cfg.value?.settings_win_y ?? -1
    openWindow(kind, x, y).catch(() => {})
  }

  /**
   * 保存配置并把结果合并回本地快照。
   *
   * 必须回写：`rememberWindowRect` 的"变了才写"是拿 cfg 里的旧坐标比的，
   * 不回写的话比对永远成立，每个窗口 10 秒一次往磁盘写一遍完全相同的值
   * ——三个窗口叠加就是每分钟十几次来回重写 config.json，纯属给文件写坏
   * 制造机会。
   */
  async function saveAndSync(patch: Partial<ClientConfig>) {
    try {
      await api.saveConfig(patch)
      if (opts.cfg.value) Object.assign(opts.cfg.value, patch)
    } catch {
      /* 位置/贴边记忆失败不打扰员工 */
    }
  }

  /** 定期把窗口位置存进配置，下次启动恢复（值没变就不写） */
  async function rememberWindowRect() {
    if (!hasTauri() || docked.value) return
    const rect = await getWindowRect()
    if (!rect) return
    const [x, y] = rect
    if (opts.route.value === 'history') {
      if (x !== opts.cfg.value?.history_win_x || y !== opts.cfg.value?.history_win_y) {
        await saveAndSync({ history_win_x: x, history_win_y: y })
      }
    } else if (opts.route.value === 'settings') {
      if (x !== opts.cfg.value?.settings_win_x || y !== opts.cfg.value?.settings_win_y) {
        await saveAndSync({ settings_win_x: x, settings_win_y: y })
      }
    } else if (x !== opts.cfg.value?.win_x || y !== opts.cfg.value?.win_y) {
      await saveAndSync({ win_x: x, win_y: y })
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

  /** 设置保存后的窗口类偏好（只有主窗能应用：命令在 Rust 里固定操作 main 标签） */
  function applyWindowConfigDiff(prev: ClientConfig | null, next: ClientConfig) {
    if (!hasTauri()) return
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

  /**
   * 本地服务就绪后的窗口三件套监听：贴边状态回写、位置记忆定时器、
   * 拖到边缘自动贴边（监听窗口移动，停止 300ms 后让 Rust 判边缘距离。
   * 启动期的程序化位移不算数，就绪 1s 后才武装）。
   */
  function startListeners() {
    listenDockChanged((side) => {
      docked.value = side !== null
      // 只记住"贴在哪一边"：设置里已经不提供方向选择，这项纯粹是下次启动还原用
      saveAndSync({ dock_side: side ?? '' })
    })
    rectTimer = window.setInterval(rememberWindowRect, 10_000)

    if (opts.cfg.value?.auto_dock !== false) {
      listenWindowMoved(() => {
        if (!autoDockArmed) return
        if (moveTimer) window.clearTimeout(moveTimer)
        moveTimer = window.setTimeout(() => {
          checkAutoDock().catch(() => {})
        }, 300)
      })
      window.setTimeout(() => (autoDockArmed = true), 1000)
    }
  }

  onUnmounted(() => {
    if (rectTimer) window.clearInterval(rectTimer)
    if (moveTimer) window.clearTimeout(moveTimer)
  })

  return {
    hasWindowCtl,
    pinned,
    docked,
    togglePin,
    toggleDock,
    openAux,
    applyWindowPrefs,
    applyWindowConfigDiff,
    startListeners,
  }
}
