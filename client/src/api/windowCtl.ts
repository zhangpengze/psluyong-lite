/**
 * 窗口行为控制：置顶 / 贴边 / 多窗口 / 跨窗口事件
 *
 * 依赖 tauri.conf.json 的 withGlobalTauri 注入的 window.__TAURI__。
 * 浏览器预览（vite dev 直开）没有注入，所有方法安全降级为 no-op，
 * 界面上的按钮也会隐藏——桌面客户端里才可见。
 */

type TauriGlobal = {
  core: { invoke: (cmd: string, args?: Record<string, unknown>) => Promise<unknown> }
  event: {
    listen: (ev: string, cb: (e: { payload?: unknown }) => void) => Promise<() => void>
  }
}

function tg(): TauriGlobal | null {
  return (window as unknown as { __TAURI__?: TauriGlobal }).__TAURI__ ?? null
}

export function hasTauri(): boolean {
  return tg() !== null
}

export type DockSide = '' | 'left' | 'right' | 'top'

/** 主窗置顶（偏好由 Rust 记住，辅助窗关闭时恢复） */
export async function setMainAlwaysOnTop(on: boolean): Promise<void> {
  await tg()?.core.invoke('set_main_always_on_top', { on })
}

/** 主窗贴边开关：空串 = 取消贴边。设置页改方向也走这里 */
export async function setMainDock(side: DockSide): Promise<void> {
  await tg()?.core.invoke('set_main_dock', { side })
}

/** 拖拽稳定后的自动贴边判定（Rust 里判 24px 边缘距离） */
export async function checkAutoDock(): Promise<void> {
  await tg()?.core.invoke('check_auto_dock')
}

/** 打开（或聚焦）记录窗 / 设置窗；x/y 为 -1 时居中 */
export async function openWindow(kind: 'history' | 'settings', x = -1, y = -1): Promise<void> {
  await tg()?.core.invoke('open_aux_window', { kind, x, y })
}

/** 隐藏主窗（Esc 用；托盘显隐走 Rust 自己处理） */
export async function hideMainWindow(): Promise<void> {
  await tg()?.core.invoke('hide_main_window')
}

/** 关闭主窗是否入托盘（设置页开关，同步给 Rust） */
export async function setCloseToTray(on: boolean): Promise<void> {
  await tg()?.core.invoke('set_close_to_tray', { on })
}

/** 托盘气泡：今日进度 + 待补传 */
export async function setTrayTooltip(text: string): Promise<void> {
  await tg()?.core.invoke('set_tray_tooltip', { text })
}

/** 广播事件给所有窗口（生成完成、配置变更） */
export async function broadcastEvent(name: string, payload: unknown = {}): Promise<void> {
  await tg()?.core.invoke('broadcast_event', { name, payload })
}

/** [x, y, width, height] 物理像素；非 Tauri 环境返回 null */
export async function getWindowRect(): Promise<[number, number, number, number] | null> {
  if (!hasTauri()) return null
  try {
    return (await tg()!.core.invoke('get_window_rect')) as [number, number, number, number]
  } catch {
    return null
  }
}

export async function restoreWindowRect(x: number, y: number): Promise<void> {
  await tg()?.core.invoke('restore_window_rect', { x, y })
}

/** 贴边被取消（用户把窗口拖离了边缘）时回调 */
export function listenDockChanged(cb: (side: string | null) => void): void {
  tg()?.event.listen('dock-changed', (e) => {
    const payload = e.payload as { side?: string | null } | undefined
    cb(payload?.side ?? null)
  })
}

/** 生成中点了关闭按钮：Rust 拦下了，这里弹确认 */
export function listenCloseWhileBusy(cb: () => void): void {
  tg()?.event.listen('close-requested-while-busy', () => cb())
}

/** 把「是否在生成」告诉 Rust 外壳，关窗与贴边缩回时由它决定行为 */
export async function setGenerating(busy: boolean): Promise<void> {
  await tg()?.core.invoke('set_generating', { busy })
}

/** 解除拦截并真正退出（员工确认「生成中也要退出」后调用） */
export async function forceQuit(): Promise<void> {
  await tg()?.core.invoke('force_close')
}

/** 跨窗口事件：record-added / config-changed 等 */
export function listenAppEvent(name: string, cb: (payload: unknown) => void): void {
  tg()?.event.listen(name, (e) => cb(e.payload))
}

/** 主窗被拖动（含原生标题栏拖动）：防抖稳定后判自动贴边 */
export function listenWindowMoved(cb: () => void): void {
  tg()?.event.listen('tauri://move', () => cb())
}
