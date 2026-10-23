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

/** MM-DD HH:mm（本地），解析失败时原样返回，便于发现脏数据 */
export function formatTime(iso: string): string {
  const d = parseTime(iso)
  if (!d) return iso
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(
    d.getHours(),
  ).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
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
 * 按天分组：今天 / 昨天 / 具体日期（几月几日 周X）。
 * 组间顺序按记录出现顺序（服务端已按时间倒序），组内同样保持原顺序。
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
    let label: string
    if (key === todayKey) label = '今天'
    else if (key === yesterdayKey) label = '昨天'
    else {
      const d = new Date(`${key}T00:00:00`)
      label = Number.isNaN(d.getTime()) ? key : `${d.getMonth() + 1}月${d.getDate()}日 周${WEEKDAYS[d.getDay()]}`
    }
    return { key, label, list }
  })
}
