/**
 * 生成任务生命周期（composable）
 *
 * 一次「生成录用图」从提交 → 轮询 worker → 取消 → 结果落定的全过程都在这里：
 * 计时、心跳、90 秒后的取消入口、结果态。表单字段本身留在 RecordForm，
 * 这里只通过 options 读写它们——任务编排与表单录入是两件事。
 *
 * 取消入口的由来：正常生成 5~20 秒；等过 90 秒说明 PS 卡在对话框/大模板/
 * 被占用上，再等下去只是浪费时间。这时候把主按钮换成「取消生成」，
 * 员工才有自救手段——之前没有这个入口，唯一出路是关掉整个软件。
 */

import { computed, onUnmounted, ref, type Ref } from 'vue'
import { api, type ClientConfig, type ExportResult } from '../api/sidecar'
import { useConfirm } from './useConfirm'
import { useToast } from './useToast'

const CANCEL_AFTER_S = 90

export interface ExportTaskOptions {
  /** 表单字段（由 RecordForm 持有）：任务读它们拼请求，成功后按配置清空 */
  templateId: Ref<string>
  author: Ref<string>
  title: Ref<string>
  partner: Ref<string>
  /** 当前配置（auto_clear / last_* 记忆用），异步到的可能还是 null */
  config: () => ClientConfig | null
  /** 提交前的最后一道闸：表单没填全时直接不发请求 */
  canStart: () => boolean
  /** 成功瞬间：通知父组件（emit exported）并清草稿 */
  onExported: () => void
  /** 成功落定：结果条滚进可视区（表单已清空，员工一眼看到成品） */
  scrollResult: () => void
  /** busy 解除后聚焦作者框（连续录入）；禁用中的 input 接收不了焦点，必须等解除 */
  focusAuthor: () => void
}

export function useExportTask(opts: ExportTaskOptions) {
  const { toast } = useToast()
  const { confirm } = useConfirm()

  const busy = ref(false)
  const result = ref<ExportResult | null>(null)
  const elapsed = ref(0)
  const heartbeatStale = ref(false)
  const taskId = ref('')
  const cancelling = ref(false)
  let timer: number | undefined
  let pollTimer: number | undefined

  const cancelVisible = computed(() => busy.value && elapsed.value >= CANCEL_AFTER_S)
  const canCancel = computed(() => cancelVisible.value && !cancelling.value)

  /** 生成阶段文案：worker 只上报 running，阶段按耗时估算，给员工一个"在动"的感知 */
  function phaseText(s: number): string {
    if (s < 3) return '正在连接 Photoshop…'
    if (s < 8) return '正在打开模板…'
    if (s < 16) return '正在替换文字…'
    return '正在导出图片…'
  }

  /** 估算进度：基准 18 秒到 92%，完成后直接 100%，绝不假报 100% */
  const progressPct = computed(() => Math.min(92, Math.round((elapsed.value / 18) * 100)))

  function stopTimers() {
    if (timer) window.clearInterval(timer)
    if (pollTimer) window.clearTimeout(pollTimer)
  }

  function poll(id: string): Promise<ExportResult> {
    return new Promise((resolve, reject) => {
      const tick = async () => {
        try {
          const s = await api.exportStatus(id)
          if (s.finished) {
            resolve(s)
          } else {
            // 心跳恢复后提示要能消失，不能 once-true 永远挂着
            heartbeatStale.value = 'heartbeat_stale' in s ? !!s.heartbeat_stale : false
            pollTimer = window.setTimeout(tick, 1500)
          }
        } catch (e) {
          reject(e)
        }
      }
      tick()
    })
  }

  async function submit() {
    if (!opts.canStart()) return

    busy.value = true
    result.value = null
    elapsed.value = 0
    heartbeatStale.value = false
    taskId.value = ''
    // 生成通常 5-20 秒，给个跳动的秒数，员工不会以为卡死
    timer = window.setInterval(() => (elapsed.value += 1), 1000)

    try {
      const start = await api.exportImage({
        template_id: opts.templateId.value,
        author: opts.author.value.trim(),
        title: opts.title.value.trim(),
        partner_name: (opts.partner.value || '').trim(),
      })
      if (!start.ok) {
        // 409 busy：上一次还没结束，明确告诉员工现状
        result.value = { ok: false, error: start.error }
        return
      }
      taskId.value = start.task_id

      // 轮询 worker 子进程：setTimeout 链式调度，上一次没回来绝不叠加请求
      const finalRes = await poll(start.task_id)
      result.value = finalRes

      if (finalRes.ok) {
        opts.onExported()
        // 记住这次的组合（作者也记，下次打开自动带出）
        await api.saveConfig({
          last_template_id: opts.templateId.value,
          last_partner: opts.partner.value,
          last_author: opts.author.value.trim(),
        })
        if (opts.config()?.auto_clear) {
          opts.author.value = ''
          opts.title.value = ''
        }
        toast(
          finalRes.uploaded ? '生成成功，已同步到服务器' : '生成成功，已存本地（联网后自动补传）',
          finalRes.uploaded ? 'success' : 'warning',
        )
        await opts.scrollResult()
      } else if (finalRes.cancelled) {
        toast('已取消生成', 'warning')
      }
    } catch (e) {
      result.value = { ok: false, error: e instanceof Error ? e.message : '生成失败' }
    } finally {
      stopTimers()
      busy.value = false
      if (result.value?.ok && opts.config()?.auto_clear) opts.focusAuthor()
    }
  }

  /** 取消生成：只有等过 CANCEL_AFTER_S 才给这个入口，避免误触 */
  async function cancel() {
    if (!canCancel.value || !taskId.value) return
    const ok = await confirm({
      title: '取消这次生成',
      message:
        '这次生成会被中断，已经跑掉的时间不会回来。Photoshop 里可能留下这个模板的未保存文档，重试前记得手动关掉。',
      danger: true,
      confirmText: '取消生成',
      cancelText: '继续等待',
    })
    if (!ok) return

    cancelling.value = true
    try {
      const res = await api.cancelExport(taskId.value)
      // 成功时不用提示：轮询马上会拿到 cancelled 终态，那里统一提示一次，
      // 两个地方都弹会看到两条 toast
      if (!res.ok) toast(res.error || '取消失败，请稍后再试', 'error')
    } catch (e) {
      toast(e instanceof Error ? e.message : '取消失败', 'error')
    } finally {
      cancelling.value = false
    }
  }

  /** 手动重填：收起结果条（任务态归零交给下一次 submit） */
  function retry() {
    result.value = null
  }

  onUnmounted(stopTimers)

  return {
    busy,
    result,
    elapsed,
    heartbeatStale,
    cancelling,
    cancelVisible,
    canCancel,
    progressPct,
    phaseText,
    submit,
    cancel,
    retry,
  }
}
