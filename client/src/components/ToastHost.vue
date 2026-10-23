<script setup lang="ts">
/**
 * 全局 toast 渲染容器
 *
 * 挂在 App.vue 根节点末尾；弹出用 useToast() 返回的 toast(text, type)。
 * 三型 + info，图标为线性 SVG（currentColor 着色），3 秒自动消失。
 */
import { useToast } from '../composables/useToast'

const { toasts } = useToast()
</script>

<template>
  <transition-group name="toast" tag="div" class="toast-wrap">
    <div v-for="t in toasts" :key="t.id" class="toast" :class="t.type" :role="t.type === 'error' ? 'alert' : 'status'">
      <span class="t-ico">
        <svg v-if="t.type === 'success'" viewBox="0 0 16 16" width="13" height="13">
          <circle cx="8" cy="8" r="6.6" fill="none" stroke="currentColor" stroke-width="1.3" />
          <path d="M5 8.2l2.1 2.1 4-4.2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
        <svg v-else-if="t.type === 'warning'" viewBox="0 0 16 16" width="13" height="13">
          <path d="M8 2.2L14.6 13.4H1.4z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
          <line x1="8" y1="6.2" x2="8" y2="9.6" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
          <circle cx="8" cy="11.6" r="0.9" fill="currentColor" stroke="none" />
        </svg>
        <svg v-else-if="t.type === 'error'" viewBox="0 0 16 16" width="13" height="13">
          <circle cx="8" cy="8" r="6.6" fill="none" stroke="currentColor" stroke-width="1.3" />
          <line x1="5.8" y1="5.8" x2="10.2" y2="10.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
          <line x1="10.2" y1="5.8" x2="5.8" y2="10.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
        </svg>
        <svg v-else viewBox="0 0 16 16" width="13" height="13">
          <circle cx="8" cy="8" r="6.6" fill="none" stroke="currentColor" stroke-width="1.3" />
          <line x1="8" y1="7.2" x2="8" y2="11" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
          <circle cx="8" cy="5" r="0.9" fill="currentColor" stroke="none" />
        </svg>
      </span>
      <span class="t-text">{{ t.text }}</span>
    </div>
  </transition-group>
</template>
