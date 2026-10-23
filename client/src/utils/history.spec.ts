import { describe, expect, it } from 'vitest'
import { dateKey, formatTime, groupByDate, localDateKey, parseTime } from './history'

interface Row {
  created_at: string
  title: string
}

const row = (created_at: string, title = 'x'): Row => ({ created_at, title })

describe('parseTime', () => {
  it('带时区的串按原时区解析', () => {
    expect(parseTime('2026-10-21T03:00:00+00:00')?.toISOString()).toBe('2026-10-21T03:00:00.000Z')
    expect(parseTime('2026-10-21T03:00:00Z')?.toISOString()).toBe('2026-10-21T03:00:00.000Z')
  })

  it('纯时间戳按 UTC 补 Z：服务端历史数据就是这种格式', () => {
    expect(parseTime('2026-10-21T03:00:00')?.toISOString()).toBe('2026-10-21T03:00:00.000Z')
  })

  it('脏数据返回 null 而不是 Invalid Date', () => {
    expect(parseTime('not-a-date')).toBeNull()
  })
})

describe('localDateKey / dateKey', () => {
  it('用本地日期，不因时区漂到隔天', () => {
    const d = new Date(2026, 9, 21, 23, 30) // 本地 2026-10-21 23:30
    expect(localDateKey(d)).toBe('2026-10-21')
  })

  it('dateKey 对脏数据退化为前 10 个字符', () => {
    expect(dateKey('2026-10-21-garbage')).toBe('2026-10-21')
    expect(dateKey('')).toBe('')
  })
})

describe('formatTime', () => {
  it('输出 MM-DD HH:mm', () => {
    const iso = new Date(2026, 9, 21, 9, 5).toISOString()
    expect(formatTime(iso)).toBe('10-21 09:05')
  })

  it('解析失败时原样返回，方便发现脏数据', () => {
    expect(formatTime('bad')).toBe('bad')
  })
})

describe('groupByDate', () => {
  const today = new Date(2026, 9, 21, 10, 0)
  const at = (day: number, hour = 12) => new Date(2026, 9, day, hour).toISOString()

  it('今天 / 昨天 / 具体日期三等分，组内保持原顺序', () => {
    const groups = groupByDate(
      [row(at(21, 9), 'a'), row(at(21, 8), 'b'), row(at(20), 'c'), row(at(18), 'd')],
      today,
    )
    expect(groups.map((g) => g.label)).toEqual(['今天', '昨天', '10月18日 周日'])
    expect(groups[0].list.map((r) => r.title)).toEqual(['a', 'b'])
    expect(groups[2].list.map((r) => r.title)).toEqual(['d'])
  })

  it('同一天的记录合并到一组，不重复出现日期标题', () => {
    const groups = groupByDate([row(at(18)), row(at(18)), row(at(18))], today)
    expect(groups).toHaveLength(1)
    expect(groups[0].list).toHaveLength(3)
  })

  it('空列表返回空数组', () => {
    expect(groupByDate([], today)).toEqual([])
  })

  it('跨月的"昨天"也算昨天', () => {
    const oct1 = new Date(2026, 9, 1, 10, 0)
    const groups = groupByDate([row(new Date(2026, 8, 30, 12).toISOString())], oct1)
    expect(groups[0].label).toBe('昨天')
  })
})
