/**
 * 录用表单草稿与「上次组合」记忆（composable）
 *
 * 两件同源的事：员工关掉软件再回来，表单要能接着填（localStorage 草稿）；
 * 没有草稿时也要少打几个字（带出上次生成用的作者，记住上次选的模板与合作伙伴）。
 * 字段本身由 RecordForm 持有，这里只负责读写与提示。
 */

import { nextTick, onMounted, watch, type Ref } from 'vue'
import type { ClientConfig, TemplateItem } from '../api/sidecar'
import { useDraft } from './useDraft'
import { useToast } from './useToast'

export interface RecordDraftOptions {
  templateId: Ref<string>
  partner: Ref<string>
  author: Ref<string>
  title: Ref<string>
  config: () => ClientConfig | null
  templates: () => TemplateItem[]
  /** 把焦点送回作者框（打开就能打字，少一次点击） */
  focusAuthor: () => void
}

export function useRecordDraft(opts: RecordDraftOptions) {
  const { toast } = useToast()

  const draft = useDraft('record', () => ({
    template_id: opts.templateId.value,
    partner: opts.partner.value,
    author: opts.author.value,
    title: opts.title.value,
  }))

  // 记住上次用的模板与合作伙伴，第二次生成不用重新选
  watch(
    () => opts.config(),
    (cfg) => {
      if (!cfg) return
      if (!opts.templateId.value && cfg.last_template_id) opts.templateId.value = cfg.last_template_id
      if (!opts.partner.value && cfg.last_partner) opts.partner.value = cfg.last_partner
    },
    { immediate: true },
  )

  watch(
    () => opts.templates(),
    (list) => {
      if (opts.templateId.value && list.some((t) => t.id === opts.templateId.value)) return
      if (!opts.templateId.value && list.length === 1) opts.templateId.value = list[0].id
    },
  )

  /**
   * 草稿恢复提示只在「本次运行」弹一次。
   * 切标签会让本组件卸载重挂，每次都弹等于噪音（草稿内容本来就在框里看得见）。
   */
  const DRAFT_TOAST_KEY = 'psluyong-lite.draft-toast-shown'

  function notifyDraftRestored() {
    try {
      if (sessionStorage.getItem(DRAFT_TOAST_KEY) === '1') return
      sessionStorage.setItem(DRAFT_TOAST_KEY, '1')
    } catch {
      /* 存储不可用时退回每次提示，不影响功能 */
    }
    toast('已恢复上次未填写的草稿', 'info')
  }

  onMounted(() => {
    // 恢复上次未提交的草稿
    const saved = draft.load()
    if (saved) {
      opts.author.value = saved.author || ''
      opts.title.value = saved.title || ''
      opts.partner.value = saved.partner || ''
      if (saved.template_id && opts.templates().some((t) => t.id === saved.template_id)) {
        opts.templateId.value = saved.template_id
      }
      if (saved.author?.trim() || saved.title?.trim()) notifyDraftRestored()
    }
    // 草稿里没有作者时带出上次生成用的作者：同一位作者连投多篇不用重复打
    const cfg = opts.config()
    if (!opts.author.value && cfg?.last_author) opts.author.value = cfg.last_author
    // 没有作者可带出时直接聚焦作者框
    if (!opts.author.value) nextTick(() => opts.focusAuthor())
  })

  return { clearDraft: draft.clear }
}
