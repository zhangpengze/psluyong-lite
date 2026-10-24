<script setup lang="ts">
import { api, type HistoryItem } from '../api/sidecar'
import { formatTime } from '../utils/history'
import { useToast } from '../composables/useToast'
import { copyText } from '../utils/clipboard'

/**
 * 按日期分组的记录列表：整张表只有一个表头（sticky 吸顶），
 * 日期组标题变成表内的一行分隔。原来每进一个日期组就重画一次表头，
 * 三组就白花 68px 高度，一屏少看两行。
 */
const props = defineProps<{
  groups: { key: string; label: string; list: HistoryItem[] }[]
  dStart: string
  dEnd: string
}>()
const emit = defineEmits<{ 'toggle-day': [key: string]; view: [item: HistoryItem] }>()

const { toast } = useToast()

/** 是否正只看这一天 */
function isPickedDay(key: string): boolean {
  return props.dStart === key && props.dEnd === key
}

/** 列表行里的时间：带年（YYYY-MM-DD HH:mm），跨年也不会有歧义 */
function fmtShort(iso: string): string {
  return formatTime(iso)
}

function ms(processingMs?: number): string {
  return processingMs ? `${(processingMs / 1000).toFixed(1)}s` : '—'
}

async function copyName(it: HistoryItem) {
  const name = it.image_filename || ''
  if (!name) {
    toast('这条记录没有成品图', 'warning')
    return
  }
  const ok = await copyText(name)
  toast(ok ? '文件名已复制' : '复制失败，请手动复制', ok ? 'success' : 'error')
}
</script>

<template>
  <div class="card rec-card">
    <div class="rec-head">
      <span>时间</span>
      <span class="rec-c-title">标题</span>
      <span class="rec-author">作者</span>
      <span class="rec-tpl">模板</span>
      <span class="rec-pt">合作伙伴</span>
      <span>状态</span>
      <span>耗时</span>
      <span>操作</span>
    </div>
    <template v-for="g in groups" :key="g.key">
      <!-- 分组标题兼"只看这天"按钮：比让员工在两个日期框里手打快得多 -->
      <button
        class="date-group"
        :class="{ picked: isPickedDay(g.key) }"
        :title="isPickedDay(g.key) ? '取消只看这天' : `只看 ${g.key} 这天`"
        @click="emit('toggle-day', g.key)"
      >
        <span>{{ g.label }}</span>
        <span class="dg-count">{{ g.list.length }} 条</span>
      </button>
      <div v-for="it in g.list" :key="it.id" class="rec-row" :class="{ failed: it.status !== 'success' }">
        <div class="rec-main">
          <span class="rec-time">{{ fmtShort(it.created_at) }}</span>
          <span class="rec-c-title">
            <span class="rec-thumb" @click="it.has_image && emit('view', it)">
              <img v-if="it.has_image" :src="api.imageUrl(it.id, false, it.created_at)" alt="" loading="lazy" decoding="async" />
              <span v-else class="noimg">无图</span>
            </span>
            <span class="rec-title-wrap">
              <span class="rec-title-text" :title="it.title">{{ it.title || '(无标题)' }}</span>
              <span v-if="it.status !== 'success' && it.error_message" class="rec-err" :title="it.error_message">{{ it.error_message }}</span>
            </span>
          </span>
          <span class="rec-author" :title="it.author">{{ it.author || '—' }}</span>
          <span class="rec-tpl" :title="it.template_name">{{ it.template_name }}</span>
          <span class="rec-pt" :title="it.partner_name">{{ it.partner_name || '—' }}</span>
          <span class="rec-status" :class="it.status === 'success' ? 'ok' : 'bad'">
            {{ it.status === 'success' ? '成功' : '失败' }}
          </span>
          <span class="rec-ms">{{ ms(it.processing_ms) }}</span>
          <span class="rec-act">
            <a class="link" @click="copyName(it)">复制名</a>
          </span>
        </div>
      </div>
    </template>
  </div>
</template>
