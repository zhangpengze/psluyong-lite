<script setup lang="ts">
import { ref, watch } from 'vue'
import { api, type HistoryItem } from '../api/sidecar'

const props = defineProps<{ version: number; openable?: boolean }>()

const items = ref<HistoryItem[]>([])
const loading = ref(true)
const error = ref('')
const viewing = ref<HistoryItem | null>(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const res = await api.history()
    items.value = res.items
    if (!res.ok) error.value = res.error || '加载失败'
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
  }
}

watch(() => props.version, load, { immediate: true })

function fmt(iso: string): string {
  // 后端返回带 +00:00 的 UTC，纯本地时间戳则需要补 Z 才能按 UTC 解析
  const hasTz = /(Z|[+-]\d{2}:\d{2})$/.test(iso)
  const d = new Date(hasTz ? iso : `${iso}Z`)
  if (Number.isNaN(d.getTime())) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(
    d.getHours(),
  ).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
</script>

<template>
  <div>
    <div v-if="loading" class="empty">正在加载…</div>
    <div v-else-if="error" class="result fail">
      <div class="head"><span class="ico">!</span>{{ error }}</div>
    </div>
    <div v-else-if="items.length === 0" class="empty">
      还没有录用记录。<br />回到「录用」页做第一条吧。
    </div>

    <div v-else class="card">
      <h3>最近 {{ items.length }} 条</h3>
      <div v-for="it in items" :key="it.id" class="hitem">
        <img
          v-if="it.has_image"
          :src="api.imageUrl(it.id)"
          alt=""
          @click="viewing = it"
          style="cursor: zoom-in"
        />
        <div v-else class="noimg">无图</div>

        <div class="meta">
          <div class="t1">{{ it.title || '(无标题)' }}</div>
          <div class="t2">{{ it.author }} · {{ it.template_name }}</div>
          <div class="t2">
            {{ fmt(it.created_at) }} · {{ it.partner_name || '无合作伙伴' }}
            <span class="tag" :class="it.status === 'success' ? 'ok' : 'bad'">
              {{ it.status === 'success' ? '成功' : '失败' }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <div v-if="viewing" class="lightbox" @click.self="viewing = null">
      <img :src="api.imageUrl(viewing.id, true)" alt="" />
      <span class="close" @click="viewing = null">&times;</span>
    </div>
  </div>
</template>

<style scoped>
.lightbox {
  position: fixed;
  inset: 0;
  background: rgba(9, 14, 26, 0.85);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 18px;
  z-index: 99;
}
.lightbox img {
  max-width: 100%;
  max-height: 100%;
  border-radius: 8px;
}
.lightbox .close {
  position: absolute;
  top: 8px;
  right: 16px;
  color: #fff;
  font-size: 28px;
  cursor: pointer;
  line-height: 1;
}
</style>
