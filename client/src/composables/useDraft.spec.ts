import { mount } from '@vue/test-utils'
import { defineComponent, nextTick, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useDraft } from './useDraft'

type Draft = { author: string; title: string }

const KEY = 'psluyong-lite.draft.spec'

/** 用真实组件挂载：useDraft 依赖 watch / onUnmounted，必须在 setup 里跑 */
function mountForm(store: { draft?: ReturnType<typeof useDraft<Draft>> } = {}) {
  const Comp = defineComponent({
    setup() {
      const author = ref('')
      const title = ref('')
      store.draft = useDraft('spec', () => ({ author: author.value, title: title.value }))
      return { author, title }
    },
    template: '<div><input class="a" v-model="author" /><input class="t" v-model="title" /></div>',
  })
  return mount(Comp)
}

describe('useDraft', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('输入后 500ms 才落盘（防抖，不是每敲一个字都写）', async () => {
    const wrapper = mountForm()
    await wrapper.find('input.a').setValue('张三')

    expect(localStorage.getItem(KEY)).toBeNull()

    vi.advanceTimersByTime(600)
    await nextTick()
    expect(JSON.parse(localStorage.getItem(KEY)!).author).toBe('张三')
  })

  it('连续输入只保留最后一次，不会留下中间态', async () => {
    const wrapper = mountForm()
    await wrapper.find('input.a').setValue('张')
    vi.advanceTimersByTime(100)
    await wrapper.find('input.a').setValue('张三')
    vi.advanceTimersByTime(600)
    await nextTick()

    expect(JSON.parse(localStorage.getItem(KEY)!).author).toBe('张三')
  })

  it('load 读回草稿，clear 清空；损坏数据返回 null', () => {
    const store: { draft?: ReturnType<typeof useDraft<Draft>> } = {}
    mountForm(store)

    localStorage.setItem(KEY, JSON.stringify({ author: '李四', title: '深度学习' }))
    expect(store.draft!.load()).toEqual({ author: '李四', title: '深度学习' })

    localStorage.setItem(KEY, '{ 坏掉的 json')
    expect(store.draft!.load()).toBeNull()

    localStorage.setItem(KEY, JSON.stringify({ author: 'x', title: 'y' }))
    store.draft!.clear()
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('组件卸载后不再写盘（不会在别的页面继续改草稿）', async () => {
    const wrapper = mountForm()
    wrapper.unmount()
    vi.advanceTimersByTime(600)
    expect(localStorage.getItem(KEY)).toBeNull()
  })
})
