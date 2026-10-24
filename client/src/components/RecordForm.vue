<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ClientConfig, TemplateItem } from '../api/sidecar'
import { useExportTask } from '../composables/useExportTask'
import { useRecordDraft } from '../composables/useRecordDraft'
import RecordTaskStatus from './RecordTaskStatus.vue'

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

// 长度上限与参考项目一致：PS 文本图层放不下太长的文字，超出会被截断难发现
const AUTHOR_MAX = 50
const TITLE_MAX = 100

const templateId = ref('')
const partner = ref('')
const author = ref('')
const title = ref('')
const statusRef = ref<InstanceType<typeof RecordTaskStatus> | null>(null)
const authorInput = ref<HTMLInputElement | null>(null)

/**
 * 字数角标只在"快满了"的时候出现。
 * 原来输两个字就跳 2/50，一屏四个元素里唯一会动的数字全是噪音；
 * 真正需要它的时刻是 40/50、90/100——那才需要提醒"要截断了"。
 */
function nearLimit(n: number, max: number): boolean {
  return n >= max * 0.8
}

// ---------- 草稿与上次组合：中途关掉软件也能接着填 ----------
const { clearDraft } = useRecordDraft({
  templateId,
  partner,
  author,
  title,
  config: () => props.config,
  templates: () => props.templates,
  focusAuthor: () => authorInput.value?.focus(),
})

// ---------- 生成任务（提交 / 轮询 / 取消的全生命周期在 useExportTask 里） ----------
const task = useExportTask({
  templateId,
  author,
  title,
  partner,
  config: () => props.config,
  canStart: () => canSubmit.value,
  onExported: () => {
    emit('exported')
    clearDraft()
  },
  scrollResult: async () => {
    await nextTick()
    statusRef.value?.reveal()
  },
  focusAuthor: () => {
    nextTick(() => authorInput.value?.focus())
  },
})
// 模板/按钮里要用的引用解构到顶层（模板只对顶层绑定做 ref 解包）
const { busy, result, elapsed, heartbeatStale, cancelVisible, canCancel, progressPct, phaseText, cancelling } = task

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
  if (busy.value) task.cancel()
  else if (needSetup.value) emit('need-settings')
  else task.submit()
}

/** 模板占位符说明：原来占一行 hint，现在挂在下拉框的 tooltip 上 */
const tplHint = computed(() =>
  props.config
    ? `「${props.config.author_placeholder}」→ 作者，「${props.config.title_placeholder}」→ 标题`
    : '',
)

// 生成中关窗要拦一下：把忙碌状态同步给外壳（App 再转给 Rust）
watch(busy, (v) => emit('busy-change', v))

/** Enter 直接提交（输入框内）；Ctrl+Enter 全局提交 */
function onKeydown(e: KeyboardEvent) {
  if (e.ctrlKey && e.key === 'Enter') {
    e.preventDefault()
    // 配置没配完时 Ctrl+Enter 也是"我现在就要生成"的意思：
    // 直接带去设置，比按下去毫无反应好
    if (needSetup.value) emit('need-settings')
    else task.submit()
    return
  }
  // 焦点在输入框里按回车 = 提交；焦点在按钮上时回车由按钮自己处理，避免双触发
  if (e.key === 'Enter' && !e.ctrlKey && !e.altKey && !e.shiftKey) {
    const el = e.target as HTMLElement | null
    if (el && el.tagName === 'INPUT') {
      e.preventDefault()
      task.submit()
    }
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
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
        <!-- 生成状态 + 结果条 + 注意事项折叠（含打开文件夹 / 复制路径 / 重填） -->
        <RecordTaskStatus
          ref="statusRef"
          :busy="busy"
          :heartbeat-stale="heartbeatStale"
          :cancel-visible="cancelVisible"
          :result="result"
          :config="config"
          @retry="task.retry"
        />

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
