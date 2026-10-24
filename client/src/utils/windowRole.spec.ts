/**
 * 队列主人判定（见 windowRole.ts 的说明）
 *
 * 这条规则只有一个问题要回答：这个窗口该不该主动往服务器写。
 * 写错一个方向都是坑——多写一次就是重复上报/提前死信，少写一次只是晚 60 秒。
 */

import { describe, expect, it } from 'vitest'
import { ownsQueue } from './windowRole'

describe('ownsQueue', () => {
  it('主窗是队列主人', () => {
    expect(ownsQueue('main')).toBe(true)
  })

  it('记录窗与设置窗都不是', () => {
    expect(ownsQueue('history')).toBe(false)
    expect(ownsQueue('settings')).toBe(false)
  })

  it('认不出来的路由宁可不补传（少补一次没关系，多写一次是脏数据）', () => {
    expect(ownsQueue('')).toBe(false)
    expect(ownsQueue('history#x')).toBe(false)
    expect(ownsQueue('MAIN')).toBe(false)
  })
})
