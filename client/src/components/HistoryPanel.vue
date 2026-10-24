<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api, type ClientConfig, type HistoryItem, type TemplateItem } from '../api/sidecar'
import { useHistoryQuery } from '../composables/useHistoryQuery'
import HistoryStates from './HistoryStates.vue'
import HistoryTable from './HistoryTable.vue'
import HistoryLightbox from './HistoryLightbox.vue'

const props = defineProps<{
  version: number
  config: ClientConfig | null
  templates: TemplateItem[]
  partners: string[]
}>()
const emit = defineEmits<{
  stats: [{ total: number; today: number }]
}>()

// 查询状态（区间/日期/筛选/加载/分组）全在 useHistoryQuery 里
const {
  RANGES,
  range,
  custom,
  dStart,
  dEnd,
  todayStr,
  keyword,
  refreshing,
  loading,
  error,
  items,
  displayItems,
  fTemplate,
  fPartner,
  fStatus,
  templateOptions,
  partnerOptions,
  summary,
  groups,
  pickQuick,
  clearDates,
  toggleDay,
  clearAllFilters,
  reload,
} = useHistoryQuery({
  version: () => props.version,
  onStats: (s) => emit('stats', s),
})

/** 大图预览中的记录 */
const viewing = ref<HistoryItem | null>(null)

function openOutDir() {
  const dir = props.config?.output_dir
  if (dir) void api.openFolder(dir)
}

function onLightboxKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && viewing.value) viewing.value = null
}

onMounted(() => window.addEventListener('keydown', onLightboxKeydown))
onUnmounted(() => window.removeEventListener('keydown', onLightboxKeydown))
</script>

<template>
  <div class="rec">
    <!-- 工具行：区间 + 自定义日期 + 条数 + 搜索 + 刷新挤在一行。
         原来这里还有一条独立的「统计带」（全部记录 / 今日生成两块大胶囊），
         它说的和筛选行右边的「显示 N 条」是同一件事，白占 40px 高度，已合并成
         区间后面那行小字。
         日期框常驻而不是收进弹层：员工抱怨的正是"想按日期看却没有入口"，
         藏起来等于没做。 -->
    <div class="rec-tools">
      <div class="range-row">
        <button
          v-for="r in RANGES"
          :key="r.key"
          class="range-chip"
          :class="{ active: !custom && range === r.key }"
          @click="pickQuick(r.key)"
        >
          {{ r.label }}
        </button>
        <span class="dlabel">日期</span>
        <span class="date-pick" :class="{ active: custom }">
          <input
            v-model="dStart"
            class="dchip"
            type="date"
            :max="todayStr"
            title="起始日期（含当天）"
            aria-label="起始日期"
          />
          <span class="dash">~</span>
          <input
            v-model="dEnd"
            class="dchip"
            type="date"
            :max="todayStr"
            title="结束日期（含当天）"
            aria-label="结束日期"
          />
          <button v-if="custom" class="dclear" title="清掉日期，回到快捷区间" aria-label="清掉日期" @click="clearDates">
            <svg viewBox="0 0 16 16" width="9" height="9">
              <line x1="3.5" y1="3.5" x2="12.5" y2="12.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
              <line x1="12.5" y1="3.5" x2="3.5" y2="12.5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
            </svg>
          </button>
        </span>
      </div>
      <div class="search">
        <span class="s-ico">
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">
            <circle cx="7" cy="7" r="4.2" />
            <line x1="10.2" y1="10.2" x2="13.6" y2="13.6" />
          </svg>
        </span>
        <input
          v-model="keyword"
          type="text"
          placeholder="搜索作者 / 标题 / 模板"
          title="作者、标题、模板、合作伙伴都能搜"
          spellcheck="false"
        />
        <button v-if="keyword" class="s-clear" title="清空搜索" aria-label="清空搜索" @click="keyword = ''">
          <svg viewBox="0 0 16 16" width="10" height="10">
            <line x1="3.5" y1="3.5" x2="12.5" y2="12.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
            <line x1="12.5" y1="3.5" x2="3.5" y2="12.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" />
          </svg>
        </button>
        <button
          class="s-clear refresh-btn"
          :class="{ spinning: refreshing }"
          :disabled="refreshing"
          title="刷新（重新从服务器拉取）"
          aria-label="刷新记录"
          @click="reload"
        >
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
            <path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.7" />
            <path d="M13.4 2.6v3.2h-3.2" />
          </svg>
        </button>
      </div>
    </div>

    <!-- 筛选行：模板 / 合作伙伴 / 状态 -->
    <div class="rec-filters">
      <span class="rf-label">筛选</span>
      <select v-model="fTemplate" class="fchip">
        <option value="">全部模板</option>
        <option v-for="t in templateOptions" :key="t" :value="t">{{ t }}</option>
      </select>
      <select v-model="fPartner" class="fchip">
        <option value="">全部合作伙伴</option>
        <option v-for="p in partnerOptions" :key="p" :value="p">{{ p }}</option>
      </select>
      <select v-model="fStatus" class="fchip">
        <option value="">全部状态</option>
        <option value="success">成功</option>
        <option value="failed">失败</option>
      </select>
      <span class="spacer" />
      <!-- 计数挂在筛选行右侧而不是区间行：区间行要塞 4 个档位 + 两个日期框 + 搜索，
          再放一段「共 N 条 · 今日 M」就会把搜索框挤到第二行（真实踩过）。 -->
      <span class="rf-count">{{ summary }}</span>
    </div>

    <!-- 加载 / 错误 / 空态（内部优先级同原先的 v-if 链），否则是记录表 -->
    <HistoryStates
      v-if="loading || !!error || items.length === 0 || displayItems.length === 0"
      :loading="loading"
      :error="error"
      :empty-total="items.length === 0"
      :empty-filtered="displayItems.length === 0"
      :custom="custom"
      :range="range"
      @reload="reload"
      @clear-filters="clearAllFilters"
    />
    <HistoryTable
      v-else
      :groups="groups"
      :d-start="dStart"
      :d-end="dEnd"
      @toggle-day="toggleDay"
      @view="viewing = $event"
    />

    <!-- 底部操作：打开本机输出目录 -->
    <div v-if="!loading && !error && items.length" class="rec-foot">
      <a class="link" @click="openOutDir">打开输出文件夹</a>
    </div>

    <HistoryLightbox v-if="viewing" :item="viewing" @close="viewing = null" />
  </div>
</template>
