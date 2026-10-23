<script setup lang="ts">
/**
 * 全局确认对话框渲染组件
 *
 * Esc = 取消；打开时焦点落在取消按钮（安全侧），Enter 确认。
 * 交互契约与 useConfirm 模块单例配合，见 useConfirm.ts。
 */
import { onMounted, onUnmounted, ref } from 'vue'
import { useConfirm } from '../composables/useConfirm'

const { confirmState, resolveConfirm } = useConfirm()
const cancelBtn = ref<HTMLButtonElement | null>(null)

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    resolveConfirm(false)
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  cancelBtn.value?.focus()
})
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div v-if="confirmState" class="modal-scrim" @click.self="resolveConfirm(false)">
    <div class="modal" role="alertdialog" aria-modal="true" :aria-label="confirmState.title || '请确认'">
      <div class="m-title">
        <span class="m-ico">
          <svg viewBox="0 0 16 16" width="12" height="12">
            <path d="M8 2.2L14.6 13.4H1.4z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
            <line x1="8" y1="6.2" x2="8" y2="9.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
            <circle cx="8" cy="11.6" r="0.9" fill="currentColor" stroke="none" />
          </svg>
        </span>
        {{ confirmState.title || '请确认' }}
      </div>
      <div class="m-msg">{{ confirmState.message }}</div>
      <div class="m-actions">
        <button ref="cancelBtn" class="btn ghost" @click="resolveConfirm(false)">
          {{ confirmState.cancelText || '取消' }}
        </button>
        <button class="btn" :class="{ danger: confirmState.danger }" @click="resolveConfirm(true)">
          {{ confirmState.confirmText || '确定' }}
        </button>
      </div>
    </div>
  </div>
</template>
