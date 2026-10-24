/**
 * 只有主窗补传（item 14）
 *
 * 三个窗口各跑一份 App.vue → 各跑一份这个 composable → 启动时全都 flushQueue。
 * 同一批积压被同时提交两三遍：5xx 的记录被各记一次失败，MAX_ATTEMPTS 提前
 * 用完，本该还能重试的记录被提前丢进死信；失败的尝试数、耗时统计也跟着虚高。
 *
 * 这里盯的是"接线"：判定函数（utils/windowRole）写对了没用，只要少写一个
 * `if (!ownsQueue(...)) return`，bug 就原样回来。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  status: vi.fn(),
  flushQueue: vi.fn(),
  history: vi.fn(),
  retryDead: vi.fn(),
  toast: vi.fn(),
  confirm: vi.fn(async () => true),
}))

vi.mock('../api/sidecar', () => ({
  api: {
    status: () => mocks.status(),
    flushQueue: () => mocks.flushQueue(),
    history: (days: string) => mocks.history(days),
    retryDead: () => mocks.retryDead(),
  },
}))

vi.mock('./useToast', () => ({ useToast: () => ({ toast: mocks.toast }) }))
vi.mock('./useConfirm', () => ({ useConfirm: () => ({ confirm: mocks.confirm }) }))

import { useSidecarStatus } from './useSidecarStatus'

function statusPayload(over: Record<string, unknown> = {}) {
  return {
    config: { theme: 'dark' },
    server_reachable: true,
    com_available: true,
    ps: null,
    pending: 2,
    dead: 0,
    pending_blocked: 0,
    blocked_reason: '',
    unsynced: 0,
    busy: false,
    config_recovered: '',
    ...over,
  }
}

const flushPayload = { sent: 1, skipped: 0, lost: 0, remaining: 0, dead: 0, blocked: 0, block_reason: '' }

function mountStatus(route: string) {
  let status!: ReturnType<typeof useSidecarStatus>
  const Host = defineComponent({
    setup() {
      status = useSidecarStatus({
        theme: ref<'dark' | 'light'>('dark'),
        applyTheme: vi.fn(),
        route: ref(route),
      })
      return () => h('div')
    },
  })
  mount(Host)
  if (!status) throw new Error('composable 没有被调用')
  return status
}

beforeEach(() => {
  mocks.status.mockResolvedValue(statusPayload())
  mocks.flushQueue.mockResolvedValue(flushPayload)
  mocks.history.mockResolvedValue({ ok: true, items: [], total: 0 })
})

afterEach(() => {
  vi.clearAllMocks()
  vi.useRealTimers()
})

describe('补传只由主窗发起', () => {
  it('主窗照常补传，数字跟着刷新', async () => {
    const st = mountStatus('main')

    await st.flushQueue()

    expect(mocks.flushQueue).toHaveBeenCalledTimes(1)
    expect(st.pending.value).toBe(0)
  })

  it('记录窗不补传', async () => {
    const st = mountStatus('history')

    await st.flushQueue()

    // 数字交给 /status 轮询，这里只管"别往服务器写"
    expect(mocks.flushQueue).not.toHaveBeenCalled()
  })

  it('设置窗也不补传', async () => {
    const st = mountStatus('settings')

    await st.flushQueue()

    expect(mocks.flushQueue).not.toHaveBeenCalled()
  })
})

describe('今日数只由主窗拉（托盘气泡只有主窗在用）', () => {
  it('主窗会拉', async () => {
    const st = mountStatus('main')

    await st.refreshToday()

    expect(mocks.history).toHaveBeenCalledTimes(1)
  })

  it('辅助窗不拉', async () => {
    const st = mountStatus('history')

    await st.refreshToday()

    expect(mocks.history).not.toHaveBeenCalled()
  })
})

describe('轮询里的"服务器恢复后自动补传"', () => {
  it('记录窗即使发现服务器恢复也不补传', async () => {
    vi.useFakeTimers()
    mocks.status.mockResolvedValue(statusPayload({ server_reachable: false }))
    const st = mountStatus('history')
    await st.refreshStatus(true) // 先记下"离线"
    expect(st.serverOk.value).toBe(false)

    st.startPolling()
    mocks.status.mockResolvedValue(statusPayload({ server_reachable: true, pending: 3 }))
    await vi.advanceTimersByTimeAsync(60_000)

    expect(mocks.flushQueue).not.toHaveBeenCalled()
  })

  it('主窗发现服务器恢复就补传', async () => {
    vi.useFakeTimers()
    mocks.status.mockResolvedValue(statusPayload({ server_reachable: false }))
    const st = mountStatus('main')
    await st.refreshStatus(true)

    st.startPolling()
    mocks.status.mockResolvedValue(statusPayload({ server_reachable: true, pending: 3 }))
    await vi.advanceTimersByTimeAsync(60_000)

    expect(mocks.flushQueue).toHaveBeenCalledTimes(1)
  })
})
