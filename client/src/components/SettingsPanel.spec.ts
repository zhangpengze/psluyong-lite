import { afterEach, describe, expect, it } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import SettingsPanel from './SettingsPanel.vue'

/** 迁移冒烟：SettingsPanel 拆出五个分组子组件后，分组切换与保存栏行为不变 */
describe('SettingsPanel（拆分后）', () => {
  let wrapper: VueWrapper | undefined
  afterEach(() => wrapper?.unmount())

  it('默认落在账号分组，切换到模板分组渲染上传区', async () => {
    wrapper = mount(SettingsPanel, {
      props: { config: null, serverOk: true, templates: [], ps: null },
    })
    expect(wrapper.text()).toContain('你的姓名')
    expect(wrapper.find('input[placeholder="向主管索取"]').exists()).toBe(true)

    const tabs = wrapper.findAll('nav.set-tabs button')
    expect(tabs.map((t) => t.text())).toEqual(['账号', '模板0', '生成', '窗口', '高级'])
    await tabs[1].trigger('click')
    expect(wrapper.find('.dropzone').exists()).toBe(true)
    expect(wrapper.text()).toContain('点击选择 PSD 文件，或拖拽到此处')
  })

  it('没有未保存改动时保存栏不出现（config 还没到时同理）', () => {
    wrapper = mount(SettingsPanel, {
      props: { config: null, serverOk: false, templates: [], ps: null },
    })
    expect(wrapper.find('.save-bar').exists()).toBe(false)
    // config 为 null 时 dirty 恒 false：改了输入框也不出保存栏（与拆分前一致）
    expect(wrapper.find('input[placeholder="例如：张三"]').exists()).toBe(true)
  })
})
