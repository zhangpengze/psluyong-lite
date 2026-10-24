/**
 * 窗口位置记忆：位置没变就不能反复写配置
 *
 * 三个窗口（主/记录/设置）都会跑 startListeners，各自每 10 秒比对一次坐标。
 * 旧实现写完不回写本地快照，比对永远"不相等"→ 每个窗口每 10 秒往
 * config.json 写一次完全相同的值。叠加起来就是每分钟十几次重写，
 * 白白给"配置文件写坏"制造机会。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'

const mocks = vi.hoisted(() => ({
  saveConfig: vi.fn(async (_patch: unknown) => ({ ok: true, config: {} })),
  getWindowRect: vi.fn(async () => [100, 200] as [number, number]),
}))

vi.mock('../api/sidecar', () => ({
  api: { saveConfig: (patch: unknown) => mocks.saveConfig(patch) },
}))

vi.mock('../api/windowCtl', () => ({
  hasTauri: () => true,
  getWindowRect: () => mocks.getWindowRect(),
  listenDockChanged: () => {},
  listenWindowMoved: () => {},
  checkAutoDock: async () => {},
  dockMainNearest: async () => {},
  openWindow: async () => {},
  restoreWindowRect: async () => {},
  setCloseToTray: async () => {},
  setMainAlwaysOnTop: async () => {},
  setMainDock: async () => {},
}))

import { useWindowControls } from './useWindowControls'
import type { ClientConfig } from '../api/sidecar'

function makeCfg(): { value: ClientConfig | null } {
  return ref({ win_x: -1, win_y: -1, history_win_x: -1, history_win_y: -1 } as unknown as ClientConfig)
}

describe('useWindowControls 位置记忆', () => {
  beforeEach(() => {
    mocks.saveConfig.mockClear()
    mocks.getWindowRect.mockClear()
    // mockClear 不清实现，上一个用例改过的返回值会漏到这里
    mocks.getWindowRect.mockResolvedValue([100, 200])
    vi.useFakeTimers()
  })

  it('位置没变只写一次（不会每 10 秒重写 config.json）', async () => {
    const controls = useWindowControls({ cfg: makeCfg() as never, route: ref('main') as never })
    controls.startListeners()

    await vi.advanceTimersByTimeAsync(10_000)
    expect(mocks.saveConfig).toHaveBeenCalledTimes(1)
    expect(mocks.saveConfig).toHaveBeenCalledWith({ win_x: 100, win_y: 200 })

    // 再过一分钟（6 个周期）位置没动，就该一次都不写
    await vi.advanceTimersByTimeAsync(60_000)
    expect(mocks.saveConfig).toHaveBeenCalledTimes(1)
    vi.useRealTimers()
  })

  it('窗口真的挪了才会再写一次', async () => {
    const controls = useWindowControls({ cfg: makeCfg() as never, route: ref('main') as never })
    controls.startListeners()

    await vi.advanceTimersByTimeAsync(10_000)
    expect(mocks.saveConfig).toHaveBeenCalledTimes(1)

    mocks.getWindowRect.mockResolvedValue([300, 400])
    await vi.advanceTimersByTimeAsync(10_000)
    expect(mocks.saveConfig).toHaveBeenCalledTimes(2)
    expect(mocks.saveConfig).toHaveBeenLastCalledWith({ win_x: 300, win_y: 400 })
    vi.useRealTimers()
  })

  it('记录窗写自己的坐标字段，不碰主窗的', async () => {
    const controls = useWindowControls({ cfg: makeCfg() as never, route: ref('history') as never })
    controls.startListeners()

    await vi.advanceTimersByTimeAsync(10_000)
    expect(mocks.saveConfig).toHaveBeenCalledWith({ history_win_x: 100, history_win_y: 200 })
    vi.useRealTimers()
  })
})
