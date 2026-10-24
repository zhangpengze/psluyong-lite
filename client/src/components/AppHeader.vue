<script setup lang="ts">
/**
 * 主窗头部：logo + 记录/设置入口 + 窗口控件 + 服务器状态（单行）。
 * 窗口控件（置顶/贴边）只在 Tauri 里出现；浏览器预览由 hasWindowCtl=false 隐藏。
 */
defineProps<{
  serverOk: boolean
  pinned: boolean
  docked: boolean
  theme: 'dark' | 'light'
  hasWindowCtl: boolean
}>()
const emit = defineEmits<{
  'open-aux': [kind: 'history' | 'settings']
  'toggle-pin': []
  'toggle-dock': []
  'toggle-theme': []
}>()
</script>

<template>
  <header class="header">
    <div class="h-logo">录</div>
    <div class="h-brand">期刊录用助手</div>

    <div class="h-actions">
      <button class="icon-btn" title="记录（独立窗口）" aria-label="打开记录窗口" @click="emit('open-aux', 'history')">
        <svg viewBox="0 0 16 16" width="13" height="13">
          <circle cx="8" cy="8" r="5.8" fill="none" stroke="currentColor" stroke-width="1.3" />
          <path d="M8 4.8V8l2.3 1.5" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
        </svg>
      </button>
      <button class="icon-btn" title="设置（独立窗口）" aria-label="打开设置窗口" @click="emit('open-aux', 'settings')">
        <svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="8" cy="8" r="2.1" />
          <path d="M8 1.9v1.8M8 12.3v1.8M1.9 8h1.8M12.3 8h1.8M3.6 3.6l1.3 1.3M11.1 11.1l1.3 1.3M12.4 3.6l-1.3 1.3M4.9 11.1l-1.3 1.3" />
        </svg>
      </button>
      <template v-if="hasWindowCtl">
        <button class="icon-btn" :class="{ active: pinned }" title="置顶窗口" aria-label="置顶窗口" @click="emit('toggle-pin')">
          <svg viewBox="0 0 16 16" width="13" height="13">
            <path d="M9.5 1.8l4.7 4.7-2.1.6-1.2 3.2-2.2-2.2-3.4 3.4 3.4-3.4-2.2-2.2 3.2-1.2z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
          </svg>
        </button>
        <button class="icon-btn" :class="{ active: docked }" title="贴边隐藏" aria-label="贴边隐藏" @click="emit('toggle-dock')">
          <svg viewBox="0 0 16 16" width="13" height="13">
            <rect x="2.2" y="2.6" width="11.6" height="10.8" rx="2" fill="none" stroke="currentColor" stroke-width="1.3" />
            <path d="M2.2 5.4h3.2v5.2H2.2" fill="none" stroke="currentColor" stroke-width="1.3" />
          </svg>
        </button>
      </template>
      <!-- 主题一键切换：设置里那份已撤，同一个开关不该有两个入口。
           用"半黑半白的圆"而不是太阳/月亮：设置图标本身就是圆+射线，
           在 360px 的头部里再来一个太阳，员工分不清谁是谁。
           当前是深是浅看窗口就知道，title 说明点下去会变成什么。 -->
      <button
        class="icon-btn"
        :title="theme === 'light' ? '切换到深色主题' : '切换到浅色主题'"
        aria-label="切换主题"
        @click="emit('toggle-theme')"
      >
        <svg viewBox="0 0 16 16" width="13" height="13">
          <circle cx="8" cy="8" r="5.9" fill="none" stroke="currentColor" stroke-width="1.3" />
          <path d="M8 2.1a5.9 5.9 0 0 0 0 11.8z" fill="currentColor" />
        </svg>
      </button>
      <span class="dot" :class="serverOk ? 'on' : 'off'" :title="serverOk ? '服务器正常' : '服务器离线，记录会存在本地'">
        {{ serverOk ? '' : '离线' }}
      </span>
    </div>
  </header>
</template>
