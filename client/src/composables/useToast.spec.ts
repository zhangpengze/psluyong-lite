import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useToast } from './useToast'

const { toasts, toast } = useToast()

describe('useToast', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    toasts.value.length = 0
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('1.2 秒内相同文本 + 相同类型不重复堆叠（连点按钮不刷屏）', () => {
    vi.setSystemTime(new Date('2026-10-21T10:00:00Z'))
    toast('已保存', 'success')
    const second = toast('已保存', 'success')

    expect(toasts.value).toHaveLength(1)
    expect(second).toBe(toasts.value[0].id)
  })

  it('同文本不同类型算两条（成功和警告语义不同）', () => {
    vi.setSystemTime(new Date('2026-10-21T10:00:00Z'))
    toast('完成', 'success')
    toast('完成', 'warning')

    expect(toasts.value).toHaveLength(2)
  })

  it('超过 1.2 秒后允许再次弹出', () => {
    vi.setSystemTime(new Date('2026-10-21T10:00:00Z'))
    toast('已保存', 'success')
    vi.setSystemTime(new Date('2026-10-21T10:00:02Z'))
    toast('已保存', 'success')

    expect(toasts.value).toHaveLength(2)
  })

  it('默认 3 秒后自动消失，duration=0 常驻', () => {
    toast('会消失', 'info')
    toast('常驻', 'error', 0)

    vi.advanceTimersByTime(3100)
    expect(toasts.value.map((t) => t.text)).toEqual(['常驻'])
  })
})
