import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises, type VueWrapper } from '@vue/test-utils'
import RecordForm from './RecordForm.vue'
import RecordTaskStatus from './RecordTaskStatus.vue'
import type { TemplateItem } from '../api/sidecar'

const TEMPLATE: TemplateItem = { id: 't1', name: '基于深度学习', filename: 'a.psd', size: 2048 }

/** 迁移冒烟：RecordForm 拆出 useExportTask / useRecordDraft / RecordTaskStatus 后行为不变 */
describe('RecordForm（拆分后）', () => {
  let wrapper: VueWrapper | undefined
  afterEach(() => wrapper?.unmount())

  it('配置没配完时主按钮变「去配置」入口，点击发 need-settings', async () => {
    wrapper = mount(RecordForm, {
      props: {
        config: null,
        templates: [],
        partners: [],
        ready: false,
        missing: ['姓名', 'API Key'],
        missingDetail: '姓名、API Key',
      },
    })
    const btn = wrapper.get('button.btn')
    expect(btn.text()).toContain('去配置：姓名 · API Key')
    expect(btn.attributes('title')).toContain('还差：姓名、API Key')
    await btn.trigger('click')
    expect(wrapper.emitted('need-settings')).toHaveLength(1)
  })

  it('填全表单后提交：本地服务不可达时落到失败结果条，busy 成对回 false', async () => {
    wrapper = mount(RecordForm, {
      props: { config: null, templates: [], partners: [], ready: true },
    })
    // 单模板到达时自动选中（与拆分前同一 watch 语义：props 变化才触发）
    await wrapper.setProps({ templates: [TEMPLATE] })
    await flushPromises()
    await wrapper.get('input[placeholder="作者 *"]').setValue('李四')
    await wrapper.get('input[placeholder="标题 *"]').setValue('基于深度学习的图像分割方法研究')
    const btn = wrapper.get('button.btn')
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    // fetch 失败的时机取决于环境，轮询等待失败结果条出现
    await vi.waitFor(() => expect(wrapper!.find('.rbar.fail').exists()).toBe(true), { timeout: 3000 })
    expect(wrapper.find('.rb-msg').text()).toContain('本地服务未启动')
    await vi.waitFor(() => {
      const busyEvents = wrapper!.emitted('busy-change')?.map((e) => e[0]) ?? []
      expect(busyEvents).toContain(true)
      expect(busyEvents[busyEvents.length - 1]).toBe(false)
    }, { timeout: 3000 })
  })

  it('状态条已独立成 RecordTaskStatus；键帽仍是文本 ↵（功能图标全为内联 SVG）', () => {
    wrapper = mount(RecordForm, {
      props: { config: null, templates: [], partners: [], ready: true },
    })
    expect(wrapper.findComponent(RecordTaskStatus).exists()).toBe(true)
    // 未生成时结果条不渲染
    expect(wrapper.find('.rbar').exists()).toBe(false)
    expect(wrapper.find('button.btn kbd').text()).toBe('Ctrl ↵')
  })
})
