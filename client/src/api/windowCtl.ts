/**
 * 窗口行为控制：置顶 / 贴边隐藏 / 位置记忆
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

export async function setAlwaysOnTop(on: boolean): Promise<void> {
  await tg()?.core.invoke('set_always_on_top', { on })
}

export async function dockWindow(side: 'left' | 'right'): Promise<void> {
  await tg()?.core.invoke('dock_window', { side })
}

export async function undockWindow(): Promise<void> {
  await tg()?.core.invoke('undock_window')
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
