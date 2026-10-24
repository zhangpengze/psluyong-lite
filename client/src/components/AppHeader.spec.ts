import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AppHeader from './AppHeader.vue'

/** 迁移冒烟：头部从 App 拆出后，入口/窗口控件/状态点与事件流不变 */
describe('AppHeader（拆分后）', () => {
  it('记录 / 设置入口各自发 open-aux', async () => {
    const wrapper = mount(AppHeader, {
      props: { serverOk: true, pinned: false, docked: false, theme: 'dark', hasWindowCtl: true },
    })
    const btns = wrapper.findAll('button.icon-btn')
    await btns[0].trigger('click')
    await btns[1].trigger('click')
    expect(wrapper.emitted('open-aux')?.map((e) => e[0])).toEqual(['history', 'settings'])
  })

  it('hasWindowCtl=false 时隐藏置顶/贴边；主题按钮说明点下去的结果', () => {
    const wrapper = mount(AppHeader, {
      props: { serverOk: false, pinned: false, docked: false, theme: 'dark', hasWindowCtl: false },
    })
    expect(wrapper.findAll('button.icon-btn')).toHaveLength(3) // 记录 / 设置 / 主题
    expect(wrapper.get('button[aria-label="切换主题"]').attributes('title')).toBe('切换到浅色主题')
    // 离线状态点带文字说明
    expect(wrapper.get('.dot').text()).toBe('离线')
    expect(wrapper.get('.dot').classes()).toContain('off')
  })

  it('置顶 / 贴边 / 主题切换各发自己的事件', async () => {
    const wrapper = mount(AppHeader, {
      props: { serverOk: true, pinned: true, docked: false, theme: 'light', hasWindowCtl: true },
    })
    await wrapper.get('button[aria-label="置顶窗口"]').trigger('click')
    await wrapper.get('button[aria-label="贴边隐藏"]').trigger('click')
    await wrapper.get('button[aria-label="切换主题"]').trigger('click')
    expect(wrapper.emitted('toggle-pin')).toHaveLength(1)
    expect(wrapper.emitted('toggle-dock')).toHaveLength(1)
    expect(wrapper.emitted('toggle-theme')).toHaveLength(1)
    // 在线状态点没有文字，只靠颜色类表达
    expect(wrapper.get('.dot').text()).toBe('')
    expect(wrapper.get('.dot').classes()).toContain('on')
  })
})
