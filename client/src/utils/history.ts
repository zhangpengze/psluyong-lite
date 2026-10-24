/**
 * 记录时间与分组的纯函数
 *
 * 从 HistoryPanel 抽出来的原因：这段"UTC 补 Z""今天/昨天/周几"的判定是最容易
 * 出错又最该有测试的逻辑，挂在组件里只能靠肉眼点页面验证。
 */

/** 本地日期键 YYYY-MM-DD（不经过 UTC，避免晚上 8 点后变成明天） */
export function localDateKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(
    date.getDate(),
  ).padStart(2, '0')}`
}

/** 后端时间串 -> Date。带时区（Z / +08:00）直接用；纯时间戳补 Z 按 UTC 解析 */
export function parseTime(iso: string): Date | null {
  const hasTz = /(Z|[+-]\d{2}:\d{2})$/.test(iso)
  const d = new Date(hasTz ? iso : `${iso}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

/**
 * YYYY-MM-DD HH:mm（本地），解析失败时原样返回，便于发现脏数据。
 *
 * 为什么带年：记录是长期留档的（上一版只打 MM-DD），跨年之后「01-05」到底是
 * 今年还是去年完全看不出来，而员工找的往往正是"去年投的那篇"。
 */
export function formatTime(iso: string): string {
  const d = parseTime(iso)
  if (!d) return iso
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/** 本地日期键，用于按天分组 */
export function dateKey(iso: string): string {
  const d = parseTime(iso)
  return d ? localDateKey(d) : iso.slice(0, 10)
}

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六']

export interface DateGroup<T> {
  key: string
  label: string
  list: T[]
}

/**
 * 按天分组：今天 / 昨天 / 具体日期。
 * 组间顺序按记录出现顺序（服务端已按时间倒序），组内同样保持原顺序。
 *
 * 标签一律带年份：`今天 · 2026年10月23日 周五`、`2026年10月18日 周日`。
 * 旧版只有「10月18日」，跨年后同一天分不清哪年——而分组标签正是员工
 * 判断"这是哪一批"的唯一锚点。
 */
export function groupByDate<T extends { created_at: string }>(
  items: T[],
  today: Date = new Date(),
): DateGroup<T>[] {
  const todayKey = localDateKey(today)
  const yesterdayDate = new Date(today)
  yesterdayDate.setDate(yesterdayDate.getDate() - 1)
  const yesterdayKey = localDateKey(yesterdayDate)

  const map = new Map<string, T[]>()
  for (const item of items) {
    const key = dateKey(item.created_at)
    const bucket = map.get(key)
    if (bucket) bucket.push(item)
    else map.set(key, [item])
  }

  return [...map.entries()].map(([key, list]) => {
    const d = new Date(`${key}T00:00:00`)
    const full = Number.isNaN(d.getTime())
      ? key
      : `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 周${WEEKDAYS[d.getDay()]}`
    let label = full
    if (key === todayKey) label = `今天 · ${full}`
    else if (key === yesterdayKey) label = `昨天 · ${full}`
    return { key, label, list }
  })
}
