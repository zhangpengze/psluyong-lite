/**
 * 生成任务：成功之后"记住这次组合"失败，不能把结论翻成失败。
 *
 * 现场是这样的：图已经导出到输出目录、记录也已经上报，只剩"把上次用的模板/
 * 作者记下来"这一步。它在主 try 里，saveConfig 一抛（磁盘满、文件被占用、
 * 配置被别处改坏）→ catch 把 result 改成失败 → 员工看到"生成失败"必然再点
 * 一次 → 撞 409（当天重复提交）→ 白跑一轮 Photoshop，还多一条 failed
 * 污染失败率。整条链的起点只是一个"顺手记一下"。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent, h, ref } from 'vue'
import { mount } from '@vue/test-utils'

const mocks = vi.hoisted(() => ({
  exportImage: vi.fn(),
  exportStatus: vi.fn(),
  saveConfig: vi.fn(),
  cancelExport: vi.fn(),
  toast: vi.fn(),
  confirm: vi.fn(async () => true),
}))

vi.mock('../api/sidecar', () => ({
  api: {
    exportImage: (p: unknown) => mocks.exportImage(p),
    exportStatus: (id: string) => mocks.exportStatus(id),
    saveConfig: (p: unknown) => mocks.saveConfig(p),
    cancelExport: (id: string) => mocks.cancelExport(id),
  },
}))

vi.mock('./useToast', () => ({ useToast: () => ({ toast: mocks.toast }) }))
vi.mock('./useConfirm', () => ({ useConfirm: () => ({ confirm: mocks.confirm }) }))

import { useExportTask } from './useExportTask'
import type { ClientConfig } from '../api/sidecar'

function okResult(over: Record<string, unknown> = {}) {
  return {
    ok: true,
    finished: true,
    task_id: 'task-1',
    image_path: 'C:/期刊录用/李四.jpg',
    image_name: '李四.jpg',
    warnings: [],
    duration_ms: 8000,
    uploaded: true,
    queued: false,
    upload_message: '',
    record_id: 1,
    ...over,
  }
}

function makeOpts(over: Record<string, unknown> = {}) {
  const author = ref('李四')
  const title = ref('基于深度学习的图像分割方法研究')
  const opts = {
    templateId: ref('tpl-1'),
    author,
    title,
    partner: ref('华东出版社'),
    config: () => ({ auto_clear: true }) as unknown as ClientConfig,
    canStart: () => true,
    onExported: vi.fn(),
    scrollResult: vi.fn(async () => {}),
    focusAuthor: vi.fn(),
    ...over,
  }
  return { opts, author, title }
}

/**
 * 在真组件里调用 composable。
 *
 * 直接调会报警「onUnmounted 没有活动组件」，那个警告背后是更实际的问题：
 * 组件的卸载清理（stopTimers）根本没被注册。挂一个空组件既消掉警告，
 * 也顺带证明这个 composable 在组件里能正常用。
 */
function mountTask(opts: Record<string, unknown>) {
  let task: ReturnType<typeof useExportTask> | undefined
  const Host = defineComponent({
    setup() {
      task = useExportTask(opts as never)
      return () => h('div')
    },
  })
  mount(Host)
  if (!task) throw new Error('composable 没有被调用')
  return task
}

describe('useExportTask · 记住上次组合失败不算生成失败', () => {
  beforeEach(() => {
    mocks.exportImage.mockReset()
    mocks.exportStatus.mockReset()
    mocks.saveConfig.mockReset()
    mocks.toast.mockReset()
    mocks.exportImage.mockResolvedValue({ ok: true, task_id: 'task-1', state: 'started' })
    mocks.exportStatus.mockResolvedValue(okResult())
    mocks.saveConfig.mockResolvedValue({ ok: true, config: {} })
  })

  it('saveConfig 抛异常时，结果仍然是成功', async () => {
    mocks.saveConfig.mockRejectedValue(new Error('配置写入失败：磁盘已满'))
    const { opts } = makeOpts()
    const task = mountTask(opts)

    await task.submit()

    expect(task.result.value?.ok).toBe(true)
  })

  it('saveConfig 抛异常时，照样提示「生成成功」', async () => {
    mocks.saveConfig.mockRejectedValue(new Error('磁盘已满'))
    const { opts } = makeOpts()
    const task = mountTask(opts)

    await task.submit()

    const texts = mocks.toast.mock.calls.map((c) => String(c[0]))
    expect(texts.some((t) => t.includes('生成成功'))).toBe(true)
    expect(texts.some((t) => t.includes('失败'))).toBe(false)
  })

  it('saveConfig 抛异常时，表单仍按 auto_clear 清空（连续录入不能被打断）', async () => {
    mocks.saveConfig.mockRejectedValue(new Error('磁盘已满'))
    const { opts, author, title } = makeOpts()
    const task = mountTask(opts)

    await task.submit()

    expect(author.value).toBe('')
    expect(title.value).toBe('')
  })

  it('saveConfig 抛异常时，父组件的「已导出」通知仍然发出', async () => {
    mocks.saveConfig.mockRejectedValue(new Error('磁盘已满'))
    const { opts } = makeOpts()
    const task = mountTask(opts)

    await task.submit()

    expect(opts.onExported).toHaveBeenCalled()
  })

  it('saveConfig 正常时带上本次用的模板/伙伴/作者', async () => {
    const { opts } = makeOpts()
    const task = mountTask(opts)

    await task.submit()

    expect(mocks.saveConfig).toHaveBeenCalledWith({
      last_template_id: 'tpl-1',
      last_partner: '华东出版社',
      last_author: '李四',
    })
  })

  it('生成失败时根本不该去写配置', async () => {
    mocks.exportStatus.mockResolvedValue({
      ok: false,
      finished: true,
      task_id: 'task-1',
      error: '模板里找不到占位符',
    })
    const { opts } = makeOpts()
    const task = mountTask(opts)

    await task.submit()

    expect(task.result.value?.ok).toBe(false)
    expect(mocks.saveConfig).not.toHaveBeenCalled()
  })

  it('员工取消时也不写配置', async () => {
    mocks.exportStatus.mockResolvedValue({
      ok: false,
      finished: true,
      task_id: 'task-1',
      error: '已取消',
      cancelled: true,
    })
    const { opts } = makeOpts()
    const task = mountTask(opts)

    await task.submit()

    expect(mocks.saveConfig).not.toHaveBeenCalled()
  })
})
