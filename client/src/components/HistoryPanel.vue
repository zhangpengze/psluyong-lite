<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, type ClientConfig, type HistoryItem, type TemplateItem } from '../api/sidecar'
import { formatTime, groupByDate, localDateKey } from '../utils/history'
import { useToast } from '../composables/useToast'
import { copyText } from '../utils/clipboard'

const props = defineProps<{
  version: number
  config: ClientConfig | null
  templates: TemplateItem[]
  partners: string[]
}>()
const emit = defineEmits<{
  stats: [{ total: number; today: number }]
}>()

const { toast } = useToast()

const items = ref<HistoryItem[]>([])
const loading = ref(true)
const refreshing = ref(false)
const error = ref('')
const keyword = ref('')
const viewing = ref<HistoryItem | null>(null)
/** 时间范围：服务端默认只给近 7 天，必须显式声明区间；记录窗默认全部 */
type Range = '7' | '30' | 'all'
const RANGES: { key: Range; label: string }[] = [
  { key: '7', label: '近 7 天' },
  { key: '30', label: '近 30 天' },
  { key: 'all', label: '全部' },
]
const RANGE_KEY = 'psluyong-lite.history-range'
const range = ref<Range>((localStorage.getItem(RANGE_KEY) as Range) || 'all')
/** 服务端命中的总条数：可能大于本页拉回的条数（条数上限） */
const total = ref(0)
const rangeLabel = computed(() => RANGES.find((r) => r.key === range.value)?.label ?? '近 7 天')

// 筛选：模板 / 合作伙伴 / 状态（在已拉回的条目里过滤，选项从数据里长出来）
const fTemplate = ref('')
const fPartner = ref('')
const fStatus = ref('')
const templateOptions = computed(() => [...new Set(items.value.map((it) => it.template_name).filter(Boolean))])
const partnerOptions = computed(() => [...new Set(items.value.map((it) => it.partner_name).filter(Boolean))])

/** 今日日期（本地）：work_date 是服务端按本地日期写入的 YYYY-MM-DD */
const todayStr = computed(() => localDateKey(new Date()))

const todayCount = computed(() => items.value.filter((it) => it.work_date === todayStr.value).length)

const displayItems = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return items.value.filter(
    (it) =>
      (!kw ||
        it.author.toLowerCase().includes(kw) ||
        it.title.toLowerCase().includes(kw) ||
        it.template_name.toLowerCase().includes(kw) ||
        (it.partner_name || '').toLowerCase().includes(kw)) &&
      (!fTemplate.value || it.template_name === fTemplate.value) &&
      (!fPartner.value || it.partner_name === fPartner.value) &&
      (!fStatus.value || it.status === fStatus.value),
  )
})

/** silent：手动刷新时保留当前列表，不闪骨架屏 */
async function load(silent = false) {
  if (silent) {
    refreshing.value = true
  } else {
    loading.value = true
  }
  error.value = ''
  try {
    const res = await api.history(range.value)
    items.value = res.items ?? []
    total.value = res.total ?? items.value.length
    if (!res.ok) error.value = res.error || '加载失败'
  } catch (e) {
    error.value = e instanceof Error ? e.message : '加载失败'
  } finally {
    loading.value = false
    refreshing.value = false
  }
}

/** 手动刷新：保留当前列表，不闪骨架屏 */
function reload() {
  void load(true)
}

// 回调形式调用 load()：直接把 load 交给 watch 会把 (newVal, oldVal) 当 silent 传进去
watch([() => props.version, range], () => load())
watch(range, (r) => {
  try {
    localStorage.setItem(RANGE_KEY, r)
  } catch {
    /* 存储不可用仅本次会话记住 */
  }
})
watch([items, todayCount], () => emit('stats', { total: items.value.length, today: todayCount.value }))

function fmt(iso: string): string {
  return formatTime(iso)
}

/** 列表行里的短时间：MM-DD HH:mm */
function fmtShort(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function ms(processingMs?: number): string {
  return processingMs ? `${(processingMs / 1000).toFixed(1)}s` : '—'
}

/** 日期分组：今天 / 昨天 / 具体日期，组内保持服务端返回顺序 */
const groups = computed(() => groupByDate(displayItems.value))

async function copyName(it: HistoryItem) {
  const name = it.image_filename || ''
  if (!name) {
    toast('这条记录没有成品图', 'warning')
    return
  }
  const ok = await copyText(name)
  toast(ok ? '文件名已复制' : '复制失败，请手动复制', ok ? 'success' : 'error')
}

function openOutDir() {
  const dir = props.config?.output_dir
  if (dir) void api.openFolder(dir)
}

function onLightboxKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && viewing.value) viewing.value = null
}

onMounted(() => {
  window.addEventListener('keydown', onLightboxKeydown)
  // 首次挂载自己拉一次：watch 只在 version/range 变化时触发
  void load()
})
onUnmounted(() => window.removeEventListener('keydown', onLightboxKeydown))
</script>

<template>
  <div class="rec">
    <!-- 统计：范围条数 + 今日 -->
    <div class="stats-row">
      <div class="stat-pill">
        <span class="sp-label">{{ rangeLabel }}</span>
        <span class="sp-value">{{ total }}</span>
      </div>
      <div class="stat-pill accent">
        <span class="sp-label">今日生成</span>
        <span class="sp-value">{{ todayCount }}</span>
      </div>
    </div>

    <!-- 工具行：区间 + 搜索 + 刷新 -->
    <div class="rec-tools">
      <div class="range-row">
        <button
          v-for="r in RANGES"
          :key="r.key"
          class="range-chip"
          :class="{ active: range === r.key }"
          @click="range = r.key"
        >
          {{ r.label }}
        </button>
        <span v-if="total > items.length" class="range-more">共 {{ total }} 条 · 显示最近 {{ items.length }} 条</span>
      </div>
      <div class="search">
        <span class="s-ico">
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">
            <circle cx="7" cy="7" r="4.2" />
            <line x1="10.2" y1="10.2" x2="13.6" y2="13.6" />
          </svg>
        </span>
        <input v-model="keyword" type="text" placeholder="搜索作者 / 标题 / 模板 / 合作伙伴" spellcheck="false" />
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
      <span class="rf-count">显示 {{ displayItems.length }} 条</span>
    </div>

    <!-- 骨架屏：避免"正在加载…"白屏感 -->
    <div v-if="loading" class="card">
      <div class="skeleton">
        <div class="sk thumb" />
        <div class="sk lines">
          <div class="sk line w60" />
          <div class="sk line w40" />
        </div>
      </div>
      <div class="skeleton">
        <div class="sk thumb" />
        <div class="sk lines">
          <div class="sk line w40" />
          <div class="sk line w60" />
        </div>
      </div>
      <div class="skeleton">
        <div class="sk thumb" />
        <div class="sk lines">
          <div class="sk line w60" />
          <div class="sk line w40" />
        </div>
      </div>
    </div>

    <div v-else-if="error" class="result fail">
      <div class="head"><span class="ico">!</span>{{ error }}</div>
      <div>可能是服务器离线。记录不会丢，恢复后点底部「立即补传」。</div>
      <button class="btn ghost sm" style="margin-top: 9px" @click="reload">重新加载</button>
    </div>

    <div v-else-if="items.length === 0" class="empty">
      <span class="e-ico">
        <svg viewBox="0 0 16 16" width="19" height="19">
          <rect x="2.2" y="2.2" width="11.6" height="11.6" rx="2.2" fill="none" stroke="currentColor" stroke-width="1.3" />
          <path d="M5 8.2l2 2 4-4.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
        </svg>
      </span>
      <span class="e-main">还没有录用记录</span>
      <span class="e-sub">
        回到主窗口，选模板、填作者和标题，点生成。记录会自动出现在这里。
        <template v-if="range !== 'all'">更早的记录可切到「全部」查看。</template>
      </span>
    </div>

    <div v-else-if="displayItems.length === 0" class="empty">
      <span class="e-ico">
        <svg viewBox="0 0 16 16" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round">
          <circle cx="7" cy="7" r="4.2" />
          <line x1="10.2" y1="10.2" x2="13.6" y2="13.6" />
        </svg>
      </span>
      <span class="e-main">没有匹配的记录</span>
      <span class="e-sub">换个关键词，或清掉上面的筛选条件试试。</span>
      <button class="btn ghost sm" @click="keyword = ''; fTemplate = ''; fPartner = ''; fStatus = ''">清空筛选</button>
    </div>

    <!-- 按日期分组的记录列表（表格密度，信息看全） -->
    <template v-else v-for="g in groups" :key="g.key">
      <div class="date-group">
        {{ g.label }}
        <span class="dg-count">{{ g.list.length }} 条</span>
      </div>
      <div class="card rec-card">
        <div class="rec-head">
          <span>时间</span><span class="rec-c-title">标题</span><span>模板</span><span>合作伙伴</span><span>状态</span><span>耗时</span><span>操作</span>
        </div>
        <div v-for="it in g.list" :key="it.id" class="rec-row" :class="{ failed: it.status !== 'success' }">
          <div class="rec-main">
            <span class="rec-time">{{ fmtShort(it.created_at) }}</span>
            <span class="rec-c-title">
              <span class="rec-thumb" @click="it.has_image && (viewing = it)">
                <img v-if="it.has_image" :src="api.imageUrl(it.id, false, it.created_at)" alt="" loading="lazy" decoding="async" />
                <span v-else class="noimg">无图</span>
              </span>
              <span class="rec-title-wrap">
                <span class="rec-title-text" :title="it.title">{{ it.title || '(无标题)' }}</span>
                <span v-if="it.status !== 'success' && it.error_message" class="rec-err" :title="it.error_message">{{ it.error_message }}</span>
              </span>
            </span>
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
      </div>
    </template>

    <!-- 底部操作：打开本机输出目录 -->
    <div v-if="!loading && !error && items.length" class="rec-foot">
      <a class="link" @click="openOutDir">打开输出文件夹</a>
    </div>

    <div v-if="viewing" class="lightbox" @click.self="viewing = null">
      <img :src="api.imageUrl(viewing.id, true, viewing.created_at)" alt="" />
      <div class="lb-caption">
        {{ viewing.title || '(无标题)' }}
        <div class="lb-sub">{{ viewing.author }} · {{ viewing.template_name }} · {{ fmt(viewing.created_at) }}</div>
      </div>
      <button class="lb-close" title="关闭" aria-label="关闭预览" @click="viewing = null">&times;</button>
    </div>
  </div>
</template>
