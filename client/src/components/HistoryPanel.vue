<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import {
  api,
  type ClientConfig,
  type HistoryItem,
  type HistoryRange,
  type TemplateItem,
} from '../api/sidecar'
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

/** 快捷区间档位（不含 custom：只要员工动了日期框，就自动变成自定义区间） */
type QuickRange = Exclude<HistoryRange, 'custom'>
const RANGES: { key: QuickRange; label: string }[] = [
  { key: 'today', label: '今天' },
  { key: '7', label: '近 7 天' },
  { key: '30', label: '近 30 天' },
  { key: 'all', label: '全部' },
]
const RANGE_KEY = 'psluyong-lite.history-range'
const DATES_KEY = 'psluyong-lite.history-dates'
const range = ref<QuickRange>((localStorage.getItem(RANGE_KEY) as QuickRange) || 'all')

/** 自定义起止日期（YYYY-MM-DD，与 <input type="date"> 的 value 一致） */
function loadDates(): [string, string] {
  try {
    const raw = JSON.parse(localStorage.getItem(DATES_KEY) || '{}') as { start?: string; end?: string }
    return [raw.start || '', raw.end || '']
  } catch {
    return ['', '']
  }
}
const [initStart, initEnd] = loadDates()
const dStart = ref(initStart)
const dEnd = ref(initEnd)
/** 任一头有日期 = 自定义区间（快捷档位让位，chip 全部变灰） */
const custom = computed(() => !!(dStart.value || dEnd.value))

/** 服务端命中的总条数：可能大于本页拉回的条数（条数上限） */
const total = ref(0)
/** 服务端回显的"真正生效"区间：只填起始日时它会补上结束日，界面照它显示 */
const spanStart = ref('')
const spanEnd = ref('')

// 筛选：模板 / 合作伙伴 / 状态（在已拉回的条目里过滤，选项从数据里长出来）
const fTemplate = ref('')
const fPartner = ref('')
const fStatus = ref('')
const templateOptions = computed(() => [...new Set(items.value.map((it) => it.template_name).filter(Boolean))])
const partnerOptions = computed(() => [...new Set(items.value.map((it) => it.partner_name).filter(Boolean))])

/** 今日日期（本地）：work_date 是服务端按本地日期写入的 YYYY-MM-DD */
const todayStr = computed(() => localDateKey(new Date()))

const todayCount = computed(() => items.value.filter((it) => it.work_date === todayStr.value).length)

/** 当前区间是否包含今天：不含就别在标题后面挂「今日 0」，那会被读成"今天一条都没生成" */
const includesToday = computed(
  () => !spanStart.value || !spanEnd.value || (spanStart.value <= todayStr.value && todayStr.value <= spanEnd.value),
)

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
    const res = custom.value
      ? await api.history('custom', dStart.value, dEnd.value)
      : await api.history(range.value)
    items.value = res.items ?? []
    total.value = res.total ?? items.value.length
    spanStart.value = res.start || ''
    spanEnd.value = res.end || ''
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
watch([() => props.version, range, dStart, dEnd], () => load())
watch(range, (r) => {
  try {
    localStorage.setItem(RANGE_KEY, r)
  } catch {
    /* 存储不可用仅本次会话记住 */
  }
})
watch([dStart, dEnd], ([s, e]) => {
  try {
    localStorage.setItem(DATES_KEY, JSON.stringify({ start: s, end: e }))
  } catch {
    /* 同上 */
  }
})
watch([items, todayCount], () => emit('stats', { total: items.value.length, today: todayCount.value }))

/** 点快捷档位 = 放弃自定义日期（否则 chip 显示"近 7 天"但实际查的是别的区间） */
function pickQuick(key: QuickRange) {
  dStart.value = ''
  dEnd.value = ''
  range.value = key
}

function clearDates() {
  dStart.value = ''
  dEnd.value = ''
}

/** 是否正只看这一天 */
function isPickedDay(key: string): boolean {
  return dStart.value === key && dEnd.value === key
}

/** 点日期分组标题 = 只看这天；再点一次取消（比手打日期快得多） */
function toggleDay(key: string) {
  if (isPickedDay(key)) {
    clearDates()
  } else {
    dStart.value = key
    dEnd.value = key
  }
}

function clearAllFilters() {
  keyword.value = ''
  fTemplate.value = ''
  fPartner.value = ''
  fStatus.value = ''
  clearDates()
}

function fmt(iso: string): string {
  return formatTime(iso)
}

/** 列表行里的时间：带年（YYYY-MM-DD HH:mm），跨年也不会有歧义 */
function fmtShort(iso: string): string {
  return formatTime(iso)
}

function ms(processingMs?: number): string {
  return processingMs ? `${(processingMs / 1000).toFixed(1)}s` : '—'
}

/** 筛选行右侧那行小字：区间 + 总数 + 今日，被关键词/下拉筛掉时再补一句"显示 N 条" */
const summary = computed(() => {
  const parts: string[] = []
  // 自定义区间才回显生效跨度：只填起始日时服务端会补一个结束日，不显示就对不上
  if (custom.value && spanStart.value && spanEnd.value) {
    parts.push(spanStart.value === spanEnd.value ? spanStart.value : `${spanStart.value} ~ ${spanEnd.value}`)
  }
  parts.push(`共 ${total.value} 条`)
  if (includesToday.value) parts.push(`今日 ${todayCount.value}`)
  if (displayItems.value.length !== items.value.length) parts.push(`显示 ${displayItems.value.length} 条`)
  else if (total.value > items.value.length) parts.push(`只显示最近 ${items.value.length} 条`)
  return parts.join(' · ')
})

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
        <template v-if="custom || range !== 'all'">换个时间区间，或点「全部」看更早的记录。</template>
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
      <button class="btn ghost sm" @click="clearAllFilters">清空筛选</button>
    </div>

    <!-- 按日期分组的记录列表：整张表只有一个表头（sticky 吸顶），
         日期组标题变成表内的一行分隔。原来每进一个日期组就重画一次表头，
         三组就白花 68px 高度，一屏少看两行。 -->
    <div v-else class="card rec-card">
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
          @click="toggleDay(g.key)"
        >
          <span>{{ g.label }}</span>
          <span class="dg-count">{{ g.list.length }} 条</span>
        </button>
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
