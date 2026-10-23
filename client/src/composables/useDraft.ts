/**
 * 表单草稿（localStorage）
 *
 * 录用页的作者/标题/合作伙伴/模板定时存本地，员工中途关掉软件、
 * 甚至误触关闭，下次打开都能接着填。比参考项目的 sidecar RPC 方案
 * 更轻：不新增后端接口，刷新页面同样有效。
 *
 * 用法：
 *   const draft = useDraft('record', () => ({ author: author.value, ... }))
 *   draft.load() -> T | null   // 启动时恢复
 *   draft.clear()              // 成功提交后清掉
 */

import { onUnmounted, watch } from 'vue'

const PREFIX = 'psluyong-lite.draft.'
const DEBOUNCE_MS = 500

export interface DraftStore<T extends object> {
  load(): T | null
  clear(): void
}

export function useDraft<T extends object>(name: string, source: () => T): DraftStore<T> {
  const key = PREFIX + name
  let timer: number | undefined

  function load(): T | null {
    try {
      const raw = localStorage.getItem(key)
      if (!raw) return null
      const data = JSON.parse(raw) as T
      return data && typeof data === 'object' ? data : null
    } catch {
      return null
    }
  }

  function clear() {
    try {
      localStorage.removeItem(key)
    } catch {
      /* 隐私模式等场景下忽略 */
    }
  }

  watch(
    source,
    () => {
      window.clearTimeout(timer)
      timer = window.setTimeout(() => {
        try {
          localStorage.setItem(key, JSON.stringify(source()))
        } catch {
          /* 存储写满/禁用时静默：草稿是体验优化，不是功能依赖 */
        }
      }, DEBOUNCE_MS)
    },
    { deep: true },
  )

  onUnmounted(() => window.clearTimeout(timer))

  return { load, clear }
}
