/**
 * 轻量本地 toast 队列（模块级单例）
 *
 * 单窗口应用：所有组件共享一个队列，ToastHost.vue 负责渲染。
 * 用法：
 *   const { toast } = useToast()
 *   toast('已保存', 'success')
 */

import { ref } from 'vue'

export type ToastType = 'success' | 'warning' | 'error' | 'info'

export interface ToastItem {
  id: number
  type: ToastType
  text: string
}

const toasts = ref<ToastItem[]>([])
let seq = 0
/** 同一条文本正在展示时不再重复堆叠，避免连点刷屏 */
const lastShown = new Map<string, number>()

function dismiss(id: number) {
  const idx = toasts.value.findIndex((t) => t.id === id)
  if (idx >= 0) toasts.value.splice(idx, 1)
}

function toast(text: string, type: ToastType = 'success', duration = 3000): number {
  // 1.2 秒内相同文本 + 相同类型只更新不新增
  const key = `${type}:${text}`
  const now = Date.now()
  const last = lastShown.get(key)
  if (last !== undefined && now - last < 1200) {
    const existing = toasts.value.find((t) => `${t.type}:${t.text}` === key)
    if (existing) return existing.id
  }
  lastShown.set(key, now)

  const item: ToastItem = { id: ++seq, type, text }
  toasts.value.push(item)
  if (duration > 0) {
    window.setTimeout(() => dismiss(item.id), duration)
  }
  return item.id
}

export function useToast() {
  return { toasts, toast, dismiss }
}
