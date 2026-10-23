<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, type ClientConfig, type ExportResult, type TemplateItem } from '../api/sidecar'
import { useDraft } from '../composables/useDraft'
import { useToast } from '../composables/useToast'
import { copyText } from '../utils/clipboard'

const props = defineProps<{
  config: ClientConfig | null
  templates: TemplateItem[]
  partners: string[]
  ready: boolean
}>()

const emit = defineEmits<{ exported: []; 'need-settings': []; 'busy-change': [boolean] }>()
const { toast } = useToast()

// 长度上限与参考项目一致：PS 文本图层放不下太长的文字，超出会被截断难发现
const AUTHOR_MAX = 50
const TITLE_MAX = 100

const templateId = ref('')
const partner = ref('')
const author = ref('')
const title = ref('')
const busy = ref(false)
const result = ref<ExportResult | null>(null)
const elapsed = ref(0)
const heartbeatStale = ref(false)
let timer: number | undefined
let pollTimer: number | undefined
const resultCard = ref<HTMLElement | null>(null)
const authorInput = ref<HTMLInputElement | null>(null)
// 结果条里的注意事项默认收起，点展开
const warnOpen = ref(false)
// 空闲速查只在前 3 次启动显示，之后不再占地方
const showTips = ref(false)

const canSubmit = computed(
  () =>
    !busy.value &&
    props.ready &&
    !!templateId.value &&
    author.value.trim().length > 0 &&
    title.value.trim().length > 0,
)

/** 生成阶段文案：worker 只上报 running，阶段按耗时估算，给员工一个"在动"的感知 */
function phaseText(s: number): string {
  if (s < 3) return '正在连接 Photoshop…'
  if (s < 8) return '正在打开模板…'
  if (s < 16) return '正在替换文字…'
  return '正在导出图片…'
}
/** 估算进度：基准 18 秒到 92%，完成后直接 100%，绝不假报 100% */
const progressPct = computed(() => Math.min(92, Math.round((elapsed.value / 18) * 100)))

/** 模板占位符说明：原来占一行 hint，现在挂在下拉框的 tooltip 上 */
const tplHint = computed(() =>
  props.config
    ? `「${props.config.author_placeholder}」→ 作者，「${props.config.title_placeholder}」→ 标题`
    : '',
)

// ---------- 草稿：中途关掉软件也能接着填 ----------
const draft = useDraft('record', () => ({
  template_id: templateId.value,
  partner: partner.value,
  author: author.value,
  title: title.value,
}))

// 记住上次用的模板与合作伙伴，第二次生成不用重新选
watch(
  () => props.config,
  (cfg) => {
    if (!cfg) return
    if (!templateId.value && cfg.last_template_id) templateId.value = cfg.last_template_id
    if (!partner.value && cfg.last_partner) partner.value = cfg.last_partner
  },
  { immediate: true },
)

watch(
  () => props.templates,
  (list) => {
    if (templateId.value && list.some((t) => t.id === templateId.value)) return
    if (!templateId.value && list.length === 1) templateId.value = list[0].id
  },
)

/**
 * 草稿恢复提示只在「本次运行」弹一次。
 * 切标签会让本组件卸载重挂，每次都弹等于噪音（草稿内容本来就在框里看得见）。
 */
const DRAFT_TOAST_KEY = 'psluyong-lite.draft-toast-shown'
const TIPS_KEY = 'psluyong-lite.tips-shown'

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
    author.value = saved.author || ''
    title.value = saved.title || ''
    partner.value = saved.partner || ''
    if (saved.template_id && props.templates.some((t) => t.id === saved.template_id)) {
      templateId.value = saved.template_id
    }
    if (saved.author?.trim() || saved.title?.trim()) notifyDraftRestored()
  }
  // 草稿里没有作者时带出上次生成用的作者：同一位作者连投多篇不用重复打
  if (!author.value && props.config?.last_author) author.value = props.config.last_author
  // 没有作者可带出时直接聚焦作者框：打开就能打字，少一次点击
  if (!author.value) {
    nextTick(() => authorInput.value?.focus())
  }
  // 速查条：前 3 次启动显示，之后永久隐藏（老员工不需要，新员工看 3 次够了）
  try {
    const n = Number(localStorage.getItem(TIPS_KEY) || '0')
    if (n < 3) {
      showTips.value = true
      localStorage.setItem(TIPS_KEY, String(n + 1))
    }
  } catch {
    showTips.value = true
  }
  window.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  stopTimers()
})

// 生成中关窗要拦一下：把忙碌状态同步给外壳（App 再转给 Rust）
watch(busy, (v) => emit('busy-change', v))

function stopTimers() {
  if (timer) window.clearInterval(timer)
  if (pollTimer) window.clearTimeout(pollTimer)
}

/** Enter 直接提交（输入框内）；Ctrl+Enter 全局提交 */
function onKeydown(e: KeyboardEvent) {
  if (e.ctrlKey && e.key === 'Enter') {
    e.preventDefault()
    submit()
    return
  }
  // 焦点在输入框里按回车 = 提交；焦点在按钮上时回车由按钮自己处理，避免双触发
  if (e.key === 'Enter' && !e.ctrlKey && !e.altKey && !e.shiftKey) {
    const el = e.target as HTMLElement | null
    if (el && el.tagName === 'INPUT') {
      e.preventDefault()
      submit()
    }
  }
}

async function submit() {
  if (!canSubmit.value) return

  busy.value = true
  result.value = null
  warnOpen.value = false
  elapsed.value = 0
  heartbeatStale.value = false
  // 生成通常 5-20 秒，给个跳动的秒数，员工不会以为卡死
  timer = window.setInterval(() => (elapsed.value += 1), 1000)

  try {
    const start = await api.exportImage({
      template_id: templateId.value,
      author: author.value.trim(),
      title: title.value.trim(),
      partner_name: (partner.value || '').trim(),
    })
    if (!start.ok) {
      // 409 busy：上一次还没结束，明确告诉员工现状
      result.value = { ok: false, error: start.error }
      return
    }

    // 轮询 worker 子进程：setTimeout 链式调度，上一次没回来绝不叠加请求
    const finalRes = await poll(start.task_id)
    result.value = finalRes

    if (finalRes.ok) {
      emit('exported')
      draft.clear()
      // 记住这次的组合（作者也记，下次打开自动带出）
      await api.saveConfig({
        last_template_id: templateId.value,
        last_partner: partner.value,
        last_author: author.value.trim(),
      })
      if (props.config?.auto_clear) {
        author.value = ''
        title.value = ''
      }
      toast(
        finalRes.uploaded ? '生成成功，已同步到服务器' : '生成成功，已存本地（联网后自动补传）',
        finalRes.uploaded ? 'success' : 'warning',
      )
      // 结果滚进可视区：表单清空后员工一眼能看到成品
      await nextTick()
      resultCard.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
    }
  } catch (e) {
    result.value = { ok: false, error: e instanceof Error ? e.message : '生成失败' }
  } finally {
    stopTimers()
    busy.value = false
    // 连续录入：必须等 busy 解除（输入框重新可用）后再聚焦——
    // 禁用中的 input 接收不了焦点，focus() 会静默失败
    if (result.value?.ok && props.config?.auto_clear) {
      nextTick(() => authorInput.value?.focus())
    }
  }
}

function poll(taskId: string): Promise<ExportResult> {
  return new Promise((resolve, reject) => {
    const tick = async () => {
      try {
        const s = await api.exportStatus(taskId)
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

function openFolder() {
  const path = result.value?.ok ? result.value.image_path : props.config?.output_dir || ''
  if (path) api.openFolder(path)
}

/** 复制成品路径：剪贴板不可用时由 copyText 内部退收回调 + 选中 */
async function copyPath() {
  if (!result.value?.ok) return
  const ok = await copyText(result.value.image_path)
  toast(ok ? '路径已复制' : '复制失败，请手动复制', ok ? 'success' : 'error')
}

function retry() {
  result.value = null
}
</script>

<template>
  <div class="stack">
    <!-- 模板 + 合作伙伴：选填字段同行各半宽，标签入框；占位符说明挂 tooltip -->
    <div class="row">
      <select v-model="templateId" class="ctl" :title="tplHint" :disabled="busy || templates.length === 0">
        <option value="" disabled>
          {{ templates.length ? '请选择模板 *' : '暂无模板，请到设置上传 PSD' }}
        </option>
        <option v-for="t in templates" :key="t.id" :value="t.id">
          {{ t.name }}
        </option>
      </select>
      <input v-model="partner" class="ctl" type="text" list="partner-list" placeholder="合作伙伴（选填）" :disabled="busy" />
      <datalist id="partner-list">
        <option v-for="p in partners" :key="p" :value="p" />
      </datalist>
    </div>

    <!-- 字数角标仅在开始输入后出现，空框时零噪音 -->
    <div class="ctl-wrap">
      <input
        ref="authorInput"
        v-model="author"
        class="ctl"
        type="text"
        :maxlength="AUTHOR_MAX"
        placeholder="作者 *"
        :disabled="busy"
      />
      <span v-if="author.length" class="ctl-suffix" :class="{ over: author.length > AUTHOR_MAX }">
        {{ author.length }}/{{ AUTHOR_MAX }}
      </span>
    </div>

    <div class="ctl-wrap">
      <input
        v-model="title"
        class="ctl"
        type="text"
        :maxlength="TITLE_MAX"
        placeholder="标题 *"
        :disabled="busy"
      />
      <span v-if="title.length" class="ctl-suffix" :class="{ over: title.length > TITLE_MAX }">
        {{ title.length }}/{{ TITLE_MAX }}
      </span>
    </div>

    <!-- 主按钮：进度条并入按钮底色，阶段文案 + 秒数在按钮内 -->
    <button class="btn" :disabled="!canSubmit" @click="submit">
      <span v-if="busy" class="btn-fill" :style="{ width: progressPct + '%' }" />
      <span v-if="!busy" class="btn-label">
        <svg viewBox="0 0 16 16" width="12" height="12">
          <path d="M5 2.5l7 4.2-7 4.2v-8.4z" fill="currentColor" />
        </svg>
        生成录用图
        <kbd>Ctrl ↵</kbd>
      </span>
      <span v-else class="btn-label">{{ phaseText(elapsed) }} {{ elapsed }}s</span>
    </button>

    <div v-if="!ready" class="hint slim">
      还没配置完整，<a style="cursor: pointer" @click="emit('need-settings')">去设置</a>
    </div>

    <!-- 生成中只留一行关键警告；心跳过期时追加一句 -->
    <div v-if="busy" class="busy-line">
      生成中请不要关闭软件或操作 Photoshop<span v-if="heartbeatStale">，耗时比平时长，请确认 PS 未卡在对话框</span>
    </div>

    <!-- 空闲速查：仅前 3 次启动显示，之后不占地方 -->
    <div v-if="showTips && !result" class="tips">
      <kbd>Ctrl</kbd>+<kbd>↵</kbd> 生成 · <kbd>Esc</kbd> 隐藏窗口 · 记录与设置是独立窗口 · 断网自动补传
    </div>

    <!-- 结果条：成功/失败都压成一行，注意事项折叠 -->
    <div v-if="result" ref="resultCard" class="rbar" :class="result.ok ? 'ok' : 'fail'">
      <template v-if="result.ok">
        <span class="rb-ico">
          <svg viewBox="0 0 16 16" width="9" height="9">
            <path d="M5 8.2l2.1 2.1 4-4.2" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </span>
        <span class="rb-name" :title="`${result.image_name}（点击复制路径）`" @click="copyPath">{{ result.image_name }}</span>
        <span class="rb-meta">{{ (result.duration_ms / 1000).toFixed(1) }}s</span>
        <span class="rb-sync" :class="result.uploaded ? 'ok' : 'warn'">
          {{ result.uploaded ? '已同步' : '已存本地' }}
        </span>
        <button class="rb-btn" title="打开文件夹" aria-label="打开文件夹" @click="openFolder">
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round">
            <path d="M1.8 4.2a1 1 0 011-1h3.4l1.6 1.8h5.4a1 1 0 011 1v6a1 1 0 01-1 1h-10a1 1 0 01-1-1v-7.8z" />
          </svg>
        </button>
      </template>
      <template v-else>
        <span class="rb-ico">!</span>
        <span class="rb-msg" :title="result.error">{{ result.error }}</span>
        <button class="rb-btn" title="重新填写" aria-label="重新填写" @click="retry">
          <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M2.6 8a5.4 5.4 0 019.2-3.9M13.4 8a5.4 5.4 0 01-9.2 3.9" />
            <path d="M11.8 1.6v2.6H9.2M4.2 14.4v-2.6h2.6" />
          </svg>
        </button>
      </template>
    </div>

    <!-- 成功但带 PS 警告：折叠成一行计数，点开才看详情 -->
    <div v-if="result?.ok && result.warnings.length" class="rbar-warn">
      <button class="rw-toggle" @click="warnOpen = !warnOpen">
        ⚠ {{ result.warnings.length }} 条注意事项{{ warnOpen ? ' ▲' : ' ▼' }}
      </button>
      <ul v-if="warnOpen">
        <li v-for="w in result.warnings" :key="w">{{ w }}</li>
      </ul>
    </div>
  </div>
</template>
