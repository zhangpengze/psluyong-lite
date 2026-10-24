<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, type ClientConfig, type ExportResult, type TemplateItem } from '../api/sidecar'
import { useConfirm } from '../composables/useConfirm'
import { useDraft } from '../composables/useDraft'
import { useToast } from '../composables/useToast'
import { copyText } from '../utils/clipboard'

const props = defineProps<{
  config: ClientConfig | null
  templates: TemplateItem[]
  partners: string[]
  ready: boolean
  /** 还差的配置项短词（姓名 / 服务器地址 / API Key），印在主按钮上 */
  missing?: string[]
  /** 同一批项的完整说法，挂在按钮 title 上（按钮里放不下整句） */
  missingDetail?: string
}>()

const emit = defineEmits<{ exported: []; 'busy-change': [boolean]; 'need-settings': [] }>()
const { toast } = useToast()
const { confirm } = useConfirm()

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

/**
 * 字数角标只在"快满了"的时候出现。
 * 原来输两个字就跳 2/50，一屏四个元素里唯一会动的数字全是噪音；
 * 真正需要它的时刻是 40/50、90/100——那才需要提醒"要截断了"。
 */
function nearLimit(n: number, max: number): boolean {
  return n >= max * 0.8
}

// ---------- 取消生成 ----------
// 正常 5~20 秒；等过 90 秒说明 PS 卡在对话框/大模板/被占用上，再等下去
// 只是浪费时间。这时候把主按钮换成「取消生成」，员工才有自救手段——
// 之前没有这个入口，唯一出路是关掉整个软件。
const CANCEL_AFTER_S = 90
const taskId = ref('')
const cancelling = ref(false)

const cancelVisible = computed(() => busy.value && elapsed.value >= CANCEL_AFTER_S)
const canCancel = computed(() => cancelVisible.value && !cancelling.value)

const canSubmit = computed(
  () =>
    !busy.value &&
    props.ready &&
    !!templateId.value &&
    author.value.trim().length > 0 &&
    title.value.trim().length > 0,
)

/**
 * 配置没配完时，主按钮不再说「生成录用图」。
 *
 * 原来这件事占着窗口顶部一整条黄横幅（「还差 2 步：姓名、API Key」+ 去设置），
 * 但它和下面那个按钮说的是同一件事：现在还不能生成。首启引导本来就该长在
 * 「现在该做什么」上——所以直接把按钮换成「去配置：姓名 · API Key」，
 * 点它去设置。少一行元素，窗口就少 40px，员工也不会看到两处在讲同一件事。
 */
const needSetup = computed(() => !props.ready)

const setupLabel = computed(() => {
  const list = props.missing ?? []
  return list.length ? `去配置：${list.join(' · ')}` : '去设置'
})

const setupHint = computed(
  () => (props.missingDetail ? `还差：${props.missingDetail}（点这里去设置）` : '去设置里补齐配置'),
)

/** 生成中：只有取消可点；空闲：配置没配完 -> 恒可点（点了去设置）；否则按表单是否填全 */
const btnDisabled = computed(() =>
  busy.value ? !canCancel.value : needSetup.value ? false : !canSubmit.value,
)

/** 主按钮唯一入口：忙 -> 取消，配置缺 -> 去设置，否则 -> 提交 */
function onPrimary() {
  if (busy.value) cancel()
  else if (needSetup.value) emit('need-settings')
  else submit()
}

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
    // 配置没配完时 Ctrl+Enter 也是"我现在就要生成"的意思：
    // 直接带去设置，比按下去毫无反应好
    if (needSetup.value) emit('need-settings')
    else submit()
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
  taskId.value = ''
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
    taskId.value = start.task_id

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
    } else if (finalRes.cancelled) {
      toast('已取消生成', 'warning')
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
    <!-- 整组用 margin:auto 垂直居中，而不是靠顶部堆放。
         主窗只有 4 个元素，窗口一高就会在下面空出一大片；上下均分的留白
         看起来是"排版"，堆在顶部则是"没画完"。
         用 auto 外边距而不是 justify-content:center —— 后者在内容超出容器时
         会把顶部裁掉（结果条 + 注意事项展开时就能触发），auto 会自然塌成 0。 -->
    <div class="form-body">
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

      <!-- 字数角标仅在接近上限时出现（40/50、80/100），平时不占位置 -->
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
        <span v-if="nearLimit(author.length, AUTHOR_MAX)" class="ctl-suffix" :class="{ over: author.length > AUTHOR_MAX }">
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
        <span v-if="nearLimit(title.length, TITLE_MAX)" class="ctl-suffix" :class="{ over: title.length > TITLE_MAX }">
          {{ title.length }}/{{ TITLE_MAX }}
        </span>
      </div>

      <!-- 操作区：字段在上、主按钮在下。
           结果条排在按钮前面（DOM 顺序就是视觉顺序），第一次生成时整组会往上
           长高约 30px——但那 15 秒里员工不会去点按钮，等结果出来位置就稳定了，
           所以不需要为它预留空槽，省掉一层无谓的占位。 -->
      <div class="form-foot">
        <!-- 生成中只留一行关键警告；心跳过期时追加一句。
             配置缺项不在这里重复：那句话就在主按钮上（按钮自己变成「去配置：…」），
             同一个窗口里两处说一件事只会让 360px 更挤。 -->
        <div v-if="busy" class="busy-line">
          生成中请不要关闭软件或操作 Photoshop<span v-if="heartbeatStale">，耗时比平时长，请确认 PS 未卡在对话框</span>
          <span v-if="cancelVisible"> · 等太久可以点下方按钮取消</span>
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

        <!-- 主按钮：进度条并入按钮底色，阶段文案 + 秒数在按钮内。
             等太久时同一个位置换成「取消生成」，不额外加一个按钮。
             配置没配完时同一个位置换成「去配置：姓名 · API Key」——
             仍然是"现在唯一该点的那一下"，只是指向设置而不是生成。 -->
        <button
          class="btn"
          :class="{ 'btn-cancel': cancelVisible, 'btn-setup': !busy && needSetup }"
          :disabled="btnDisabled"
          :title="!busy && needSetup ? setupHint : ''"
          @click="onPrimary"
        >
          <span v-if="busy" class="btn-fill" :style="{ width: progressPct + '%' }" />
          <span v-if="!busy && needSetup" class="btn-label">
            <svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="8" cy="8" r="2.1" />
              <path d="M8 1.9v1.8M8 12.3v1.8M1.9 8h1.8M12.3 8h1.8M3.6 3.6l1.3 1.3M11.1 11.1l1.3 1.3M12.4 3.6l-1.3 1.3M4.9 11.1l-1.3 1.3" />
            </svg>
            {{ setupLabel }}
          </span>
          <span v-else-if="!busy" class="btn-label">
            <svg viewBox="0 0 16 16" width="12" height="12">
              <path d="M5 2.5l7 4.2-7 4.2v-8.4z" fill="currentColor" />
            </svg>
            生成录用图
            <kbd>Ctrl ↵</kbd>
          </span>
          <span v-else-if="cancelVisible" class="btn-label">
            <svg viewBox="0 0 16 16" width="11" height="11">
              <path d="M4.4 4.4l7.2 7.2M11.6 4.4l-7.2 7.2" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" />
            </svg>
            {{ cancelling ? '正在取消…' : `取消生成 ${elapsed}s` }}
          </span>
          <span v-else class="btn-label">{{ phaseText(elapsed) }} {{ elapsed }}s</span>
        </button>
      </div>
    </div>
  </div>
</template>