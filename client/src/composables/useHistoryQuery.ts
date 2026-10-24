/**
 * 记录查询状态机（composable）
 *
 * 记录窗的一切「看哪些记录」都在这里：快捷区间 / 自定义起止日期（本地记忆）、
 * 关键词与下拉筛选、加载与回显区间、汇总行、按日期分组。
 * HistoryPanel 只负责把这套状态画出来。
 */

import { computed, onMounted, ref, watch } from 'vue'
import {
  api,
  type HistoryItem,
  type HistoryRange,
} from '../api/sidecar'
import { groupByDate, localDateKey } from '../utils/history'

export interface HistoryQueryOptions {
  /** 外部版本号（主窗生成成功 +1）：变了就重拉一次 */
  version: () => number
  /** 列表变化后回传 { total, today } 给外壳（托盘气泡用） */
  onStats: (stats: { total: number; today: number }) => void
}

export function useHistoryQuery(opts: HistoryQueryOptions) {
  const items = ref<HistoryItem[]>([])
  const loading = ref(true)
  const refreshing = ref(false)
  const error = ref('')
  const keyword = ref('')

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
  watch([() => opts.version(), range, dStart, dEnd], () => load())
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
  watch([items, todayCount], () => opts.onStats({ total: items.value.length, today: todayCount.value }))

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

  onMounted(() => {
    // 首次挂载自己拉一次：watch 只在 version/range 变化时触发
    void load()
  })

  return {
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
  }
}
