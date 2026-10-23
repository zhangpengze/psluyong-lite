/**
 * 全局确认对话框（模块级单例，替代 window.confirm）
 *
 * 用法：
 *   const ok = await confirm({ title: '删除模板', message: '...', danger: true })
 * ConfirmDialog.vue 挂在 App.vue 根节点末尾负责渲染。
 */

import { ref } from 'vue'

export interface ConfirmOptions {
  title?: string
  message: string
  confirmText?: string
  cancelText?: string
  /** 危险操作：确认按钮用红色 */
  danger?: boolean
}

const state = ref<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(null)

function confirm(opts: ConfirmOptions): Promise<boolean> {
  // 已有确认框时直接关掉旧的那次（按取消处理），避免两个 Promise 互相踩
  if (state.value) {
    state.value.resolve(false)
    state.value = null
  }
  return new Promise<boolean>((resolve) => {
    state.value = { ...opts, resolve }
  })
}

function resolve(v: boolean) {
  const current = state.value
  state.value = null
  current?.resolve(v)
}

export function useConfirm() {
  return { confirmState: state, resolveConfirm: resolve, confirm }
}
