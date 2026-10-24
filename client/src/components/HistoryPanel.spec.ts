import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import HistoryPanel from './HistoryPanel.vue'
import HistoryStates from './HistoryStates.vue'
import HistoryTable from './HistoryTable.vue'

/** 迁移冒烟：HistoryPanel 拆出 useHistoryQuery + 三个子组件后状态机不变 */
describe('HistoryPanel（拆分后）', () => {
  let wrapper: VueWrapper | undefined
  afterEach(() => wrapper?.unmount())

  it('首帧显示骨架屏；sidecar 连不上时落进错误态（可重新加载）', async () => {
    wrapper = mount(HistoryPanel, {
      props: { version: 0, config: null, templates: [], partners: [] },
    })
    expect(wrapper.find('.skeleton').exists()).toBe(true)

    // load() 请求失败 -> error（失败时机取决于环境，轮询等待）
    await vi.waitFor(() => expect(wrapper!.find('.result.fail').exists()).toBe(true), { timeout: 3000 })
    expect(wrapper.findComponent(HistoryStates).exists()).toBe(true)
    expect(wrapper.text()).toContain('重新加载')

    // 错误态下不渲染记录表
    expect(wrapper.findComponent(HistoryTable).exists()).toBe(false)
  })

  it('工具行与筛选行照常渲染（区间档位、搜索、下拉）', () => {
    wrapper = mount(HistoryPanel, {
      props: { version: 0, config: null, templates: [], partners: [] },
    })
    expect(wrapper.findAll('.range-chip').map((c) => c.text())).toEqual(['今天', '近 7 天', '近 30 天', '全部'])
    expect(wrapper.find('input[placeholder="搜索作者 / 标题 / 模板"]').exists()).toBe(true)
    expect(wrapper.findAll('.fchip')).toHaveLength(3)
  })
})
