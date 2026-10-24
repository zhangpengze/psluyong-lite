<script setup lang="ts">
import { api, type HistoryItem } from '../api/sidecar'
import { formatTime } from '../utils/history'

/** 记录大图预览：点遮罩或关闭按钮收回（Esc 由 HistoryPanel 统一监听） */
defineProps<{
  item: HistoryItem
}>()
const emit = defineEmits<{ close: [] }>()

function imageUrl(item: HistoryItem): string {
  return api.imageUrl(item.id, true, item.created_at)
}

function fmt(iso: string): string {
  return formatTime(iso)
}
</script>

<template>
  <div class="lightbox" @click.self="emit('close')">
    <img :src="imageUrl(item)" alt="" />
    <div class="lb-caption">
      {{ item.title || '(无标题)' }}
      <div class="lb-sub">{{ item.author }} · {{ item.template_name }} · {{ fmt(item.created_at) }}</div>
    </div>
    <button class="lb-close" title="关闭" aria-label="关闭预览" @click="emit('close')">&times;</button>
  </div>
</template>
