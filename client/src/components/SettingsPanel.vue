<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
  api,
  type ClientConfig,
  type PsInfo,
  type TemplateItem,
} from '../api/sidecar'
import { useConfirm } from '../composables/useConfirm'
import { useToast } from '../composables/useToast'
import { copyText } from '../utils/clipboard'

const props = defineProps<{
  config: ClientConfig | null
  serverOk: boolean
  templates: TemplateItem[]
  /** 本机 Photoshop 的识别结果（由外壳的 /status 轮询带下来，不重复请求） */
  ps: PsInfo | null
}>()
const emit = defineEmits<{ saved: [ClientConfig]; 'templates-changed': [] }>()

const { confirm } = useConfirm()
const { toast } = useToast()

const form = reactive<ClientConfig>({
  server_url: '',
  api_key: '',
  employee_name: '',
  ps_path: '',
  output_dir: '',
  jpg_quality: 10,
  auto_clear: true,
  last_template_id: '',
  last_partner: '',
  last_author: '',
  partners: [],
  template_dir: '',
  author_placeholder: '{{作者}}',
  title_placeholder: '{{标题}}',
  filename_rule: 'author_title',
  always_on_top: false,
  // dock_side 只为让表单凑齐 ClientConfig 的类型而保留在这里：
  // 贴边方向已经不让人选了（拖到哪条屏缘就贴哪条，跟 QQ 隐藏一样），
  // 这一项是"上次贴到哪边"的运行状态，界面不展示、不参与 dirty、也不保存。
  dock_side: '',
  auto_dock: true,
  close_to_tray: true,
  win_x: -1,
  win_y: -1,
  history_win_x: -1,
  history_win_y: -1,
  settings_win_x: -1,
  settings_win_y: -1,
  // theme 只为凑齐 ClientConfig 的类型而留着：设置窗不再提供主题切换
  // （主窗头部有太阳/月亮按钮），表单里也不读它
  theme: 'dark',
})

/** 名单用「一行一个」的文本框维护，比一堆输入框更适合批量粘贴 */
const partnersText = ref('')

// ==================== 分组标签 ====================
// 原来 5 张卡片加一个折叠区平铺下来 1920px，窗口只有 700px，要滚 2.75 屏，
// 找「贴边方向」得先把「账号」滚过去。改成一次只显示一组，每组的量都在
// 一屏 400px 以内，设置页从此不滚。
type Tab = 'account' | 'templates' | 'generate' | 'window' | 'advanced'
const TABS: { key: Tab; label: string }[] = [
  { key: 'account', label: '账号' },
  { key: 'templates', label: '模板' },
  { key: 'generate', label: '生成' },
  // 标签名叫「窗口」而不是「外观」：主题已经搬到主窗头部，这里只剩主窗行为
  { key: 'window', label: '窗口' },
  { key: 'advanced', label: '高级' },
]
const tab = ref<Tab>('account')
/** config 是异步到的：等它到了再按"哪一项没配"决定落在哪一页 */
let tabDecided = false

const saving = ref(false)
const uploading = ref(false)
const testing = ref(false)
const showKey = ref(false)
const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

/** 「重新识别」：装完/换版本 PS 后点一下，不用重启软件 */
const detecting = ref(false)
/** 刚识别出来的结果：优先于外壳轮询带下来的 props.ps，
 *  否则点完"重新识别"要等最多 60 秒的下一次轮询，面板才肯变。 */
const psLocal = ref<PsInfo | null>(null)
const psShown = computed(() => psLocal.value ?? props.ps)
watch(
  () => props.ps,
  () => {
    // 外壳轮到了新结果就以外壳为准，避免本地那份盖掉更新的配置
    psLocal.value = null
  },
)

async function redetect() {
  detecting.value = true
  try {
    const res = await api.detectPs()
    if (res.ok) psLocal.value = res.ps
    if (res.ok && res.ps.found) flash(`已识别到：${res.ps.path}`, 'success')
    else if (res.ok) flash('仍未识别到 Photoshop，请确认已安装', 'warning')
    else flash('识别失败，请重试', 'error')
  } catch {
    flash('识别失败，请重试', 'error')
  } finally {
    detecting.value = false
  }
}

const templateCount = computed(() => props.templates.length)
const nameOk = computed(() => form.employee_name.trim().length > 0)
const urlOk = computed(() => form.server_url.trim().startsWith('http'))

/** 图片命名：只留两个选项，示例跟着选中的那个走，比两张各自带例子的卡片省 70px */
const FILENAME_OPTIONS = [
  { value: 'author', label: '作者姓名', sample: '李四.jpg' },
  { value: 'author_title', label: '作者 + 标题', sample: '李四_基于深度学习的图像分割方法研究.jpg' },
] as const
const filenameSample = computed(
  () => FILENAME_OPTIONS.find((o) => o.value === form.filename_rule)?.sample ?? FILENAME_OPTIONS[0].sample,
)

/** 路径字段必须是完整路径（Windows 盘符开头）。
 *  不拦的代价：员工填个「姓名」当时看不出问题，生成时 PS 才报
 *  「文件不存在」，谁都想不到是这里填错了（真实踩坑）。 */
const WIN_ABS = /^[A-Za-z]:[\\/]/
const outputDirOk = computed(() => !form.output_dir.trim() || WIN_ABS.test(form.output_dir.trim()))
const templateDirOk = computed(() => !form.template_dir.trim() || WIN_ABS.test(form.template_dir.trim()))
const psPathOk = computed(() => !form.ps_path.trim() || WIN_ABS.test(form.ps_path.trim()))

/** 有未保存的改动时按钮高亮，Ctrl+S 直接存 */
const dirty = computed(
  () =>
    !!props.config &&
    (form.server_url !== props.config.server_url ||
      form.api_key !== props.config.api_key ||
      form.employee_name !== props.config.employee_name ||
      form.ps_path !== props.config.ps_path ||
      form.output_dir !== props.config.output_dir ||
      Number(form.jpg_quality) !== props.config.jpg_quality ||
      form.auto_clear !== props.config.auto_clear ||
      form.template_dir !== props.config.template_dir ||
      form.author_placeholder !== props.config.author_placeholder ||
      form.title_placeholder !== props.config.title_placeholder ||
      form.filename_rule !== props.config.filename_rule ||
      form.always_on_top !== props.config.always_on_top ||
      form.auto_dock !== props.config.auto_dock ||
      form.close_to_tray !== props.config.close_to_tray ||
      // 主题不在 dirty 判定里：它由主窗头部的按钮改，设置窗只负责别把它冲掉
      partnersText.value !== (props.config.partners ?? []).join('\n')),
)

/**
 * 配置变化时同步进表单。
 * 首次填充必须做（config 是异步到的，早于它时表单只有默认值）；
 * 之后若员工已有未保存改动，就不能再覆盖——切主题、别处保存配置都会换掉
 * config 对象，覆盖等于把刚填的内容抹掉（页面被 keep-alive 缓存后更明显）。
 */
let hydrated = false
watch(
  () => props.config,
  (cfg) => {
    if (!cfg) return
    if (hydrated && dirty.value) return
    Object.assign(form, cfg)
    partnersText.value = (cfg.partners ?? []).join('\n')
    hydrated = true
    // 第一次拿到配置：缺哪一项就直接停在哪一页，省得员工自己找
    if (!tabDecided) {
      tabDecided = true
      tab.value = !cfg.employee_name || !cfg.server_url || !cfg.api_key ? 'account' : 'templates'
    }
  },
  { immediate: true },
)

function flash(text: string, type: 'success' | 'warning' | 'error' = 'success') {
  toast(text, type)
}

async function save() {
  if (!nameOk.value) {
    flash('请填写你的姓名，主管后台按姓名统计', 'error')
    tab.value = 'account'
    return
  }
  if (!urlOk.value) {
    flash('服务器地址应以 http:// 开头', 'error')
    tab.value = 'account'
    return
  }
  if (!outputDirOk.value) {
    flash('输出目录不是完整路径，应以盘符（如 D:\\）开头', 'error')
    tab.value = 'advanced'
    return
  }
  if (!templateDirOk.value) {
    flash('模板目录不是完整路径，应以盘符（如 D:\\）开头', 'error')
    tab.value = 'advanced'
    return
  }
  if (!psPathOk.value) {
    flash('Photoshop 路径不是完整路径，应以盘符（如 C:\\）开头', 'error')
    tab.value = 'advanced'
    return
  }

  saving.value = true
  try {
    const res = await api.saveConfig({
      server_url: form.server_url.trim().replace(/\/+$/, ''),
      api_key: form.api_key.trim(),
      employee_name: form.employee_name.trim(),
      ps_path: form.ps_path.trim(),
      output_dir: form.output_dir.trim(),
      jpg_quality: Number(form.jpg_quality),
      auto_clear: form.auto_clear,
      partners: partnersText.value.split('\n'),
      template_dir: form.template_dir.trim(),
      author_placeholder: form.author_placeholder.trim() || '{{作者}}',
      title_placeholder: form.title_placeholder.trim() || '{{标题}}',
      filename_rule: form.filename_rule,
      always_on_top: form.always_on_top,
      auto_dock: form.auto_dock,
      close_to_tray: form.close_to_tray,
      // 主题故意不发：设置窗可能有未保存改动停在这儿，而员工刚在主窗换了主题。
      // 带着表单里的旧 theme 一起提交，会把他刚选的覆盖回去。
      // 补丁语义是"没带的键不改"，所以这里省掉最安全。
    })
    // 服务端会校验字段范围：不 ok 就不能 emit saved，
    // 否则界面提示"已保存"而实际一个字节都没落盘
    if (!res.ok) {
      flash(res.error || '保存失败', 'error')
      return
    }
    emit('saved', res.config)
    flash('已保存')
  } catch (e) {
    flash(e instanceof Error ? e.message : '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

/** 测试连接：立刻走一次 /status，把"连不上"具体化成可行动的信息 */
async function testConnection() {
  if (!urlOk.value) {
    flash('服务器地址应以 http:// 开头', 'error')
    return
  }
  testing.value = true
  try {
    const s = await api.status()
    if (s.server_reachable) {
      flash(`连接成功（${form.server_url}）`, 'success')
    } else {
      flash('连不上服务器：请检查地址、端口和网线', 'error')
    }
  } catch (e) {
    flash(e instanceof Error ? e.message : '连接失败', 'error')
  } finally {
    testing.value = false
  }
}

function onKeydown(e: KeyboardEvent) {
  if (e.ctrlKey && e.key.toLowerCase() === 's') {
    e.preventDefault()
    save()
  }
}

// ---------- PSD 上传：按钮选择 + 拖拽 ----------
function pickFiles() {
  fileInput.value?.click()
}

async function uploadFiles(files: File[]) {
  const psd = files.filter((f) => f.name.toLowerCase().endsWith('.psd'))
  if (psd.length < files.length) {
    flash(`已忽略 ${files.length - psd.length} 个非 PSD 文件`, 'warning')
  }
  if (!psd.length) return

  uploading.value = true
  try {
    const res = await api.uploadTemplates(psd)
    emit('templates-changed')
    if (res.errors?.length) {
      flash(`已上传 ${res.saved.length} 个；${res.errors.join('；')}`, res.saved.length ? 'warning' : 'error')
    } else {
      flash(`已上传 ${res.saved.length} 个模板`, 'success')
    }
  } catch (e) {
    flash(e instanceof Error ? e.message : '上传失败', 'error')
  } finally {
    uploading.value = false
  }
}

function onPickFiles(event: Event) {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = '' // 允许连续选同一个文件
  void uploadFiles(files)
}

function onDragOver(e: DragEvent) {
  e.preventDefault()
  dragging.value = true
}
function onDragLeave() {
  dragging.value = false
}
function onDrop(e: DragEvent) {
  e.preventDefault()
  dragging.value = false
  const files = Array.from(e.dataTransfer?.files ?? [])
  if (files.length) void uploadFiles(files)
}

async function removeTemplate(item: TemplateItem) {
  const ok = await confirm({
    title: '删除模板',
    message: `删除模板「${item.name}」？已生成的图片不受影响。`,
    danger: true,
    confirmText: '删除',
  })
  if (!ok) return
  try {
    const res = await api.deleteTemplate(item.filename)
    if (res.ok) {
      emit('templates-changed')
      flash(`已删除「${item.name}」`, 'success')
    } else {
      flash(res.error || '删除失败', 'error')
    }
  } catch (e) {
    flash(e instanceof Error ? e.message : '删除失败', 'error')
  }
}

async function openDir() {
  try {
    const res = await api.openTemplateDir()
    if (!res.ok) flash(res.error || '打开目录失败', 'error')
  } catch (e) {
    flash(e instanceof Error ? e.message : '打开目录失败', 'error')
  }
}

function sizeText(bytes: number) {
  return bytes >= 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`
}

// ---------- 版本与诊断 ----------
const appVersion = __APP_VERSION__
const diag = ref<{ version: string; log_dir: string; machine: string; platform: string } | null>(null)

async function loadDiag() {
  try {
    const s = await api.status()
    diag.value = {
      version: s.version || '—',
      log_dir: s.log_dir || '',
      machine: s.machine || '',
      platform: s.platform || '',
    }
  } catch {
    diag.value = null
  }
}

/** 员工报障用的诊断文本：一次复制，主管不用再来回问环境 */
async function copyDiag() {
  const lines = [
    `期刊录用助手 客户端 v${appVersion} / 本地服务 v${diag.value?.version ?? '—'}`,
    `机器：${diag.value?.machine || '—'}`,
    `系统：${diag.value?.platform || '—'}`,
    `姓名：${form.employee_name || '(未设置)'}`,
    `服务器：${form.server_url || '(未设置)'}`,
    `模板：${templateCount.value} 个`,
    `日志：${diag.value?.log_dir || '—'}`,
  ]
  const ok = await copyText(lines.join('\n'))
  flash(ok ? '诊断信息已复制，可直接发给主管' : '复制失败，请手动截图', ok ? 'success' : 'error')
}

async function openLogs() {
  try {
    const res = await api.openLogs()
    if (!res.ok) flash(res.error || '打开日志目录失败', 'error')
  } catch (e) {
    flash(e instanceof Error ? e.message : '打开日志目录失败', 'error')
  }
}

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  void loadDiag()
})
onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="stack set">
    <!-- 分组标签：每组内容都在一屏内，设置页不再需要滚 2.75 屏 -->
    <nav class="set-tabs" role="tablist">
      <button
        v-for="t in TABS"
        :key="t.key"
        role="tab"
        :aria-selected="tab === t.key"
        :class="{ active: tab === t.key }"
        @click="tab = t.key"
      >
        {{ t.label }}<span v-if="t.key === 'templates'" class="tab-badge">{{ templateCount }}</span>
      </button>
    </nav>

    <!-- ============ 账号 ============ -->
    <div v-if="tab === 'account'" class="card">
      <div class="field">
        <label>你的姓名<span class="req">*</span></label>
        <input v-model="form.employee_name" type="text" placeholder="例如：张三" />
        <div class="hint">主管后台按这个姓名统计工作量，请与公司内部称呼一致</div>
      </div>
      <div class="field">
        <label>服务器地址<span class="req">*</span></label>
        <div class="row" style="align-items: center">
          <input v-model="form.server_url" type="text" placeholder="http://192.168.1.10:8080" style="flex: 1 1 auto; min-width: 0" />
          <button class="btn ghost sm" style="flex: 0 0 auto; width: auto; white-space: nowrap" :disabled="testing" @click="testConnection">
            {{ testing ? '测试中…' : '测试连接' }}
          </button>
        </div>
        <div class="hint">
          当前状态：<span :style="{ color: props.serverOk ? 'var(--ok)' : 'var(--err)', fontWeight: 600 }">{{ props.serverOk ? '可连接' : '连不上' }}</span>
        </div>
      </div>
      <div class="field">
        <label>API Key<span class="req">*</span></label>
        <div class="secret-wrap">
          <input v-model="form.api_key" :type="showKey ? 'text' : 'password'" placeholder="向主管索取" />
          <button class="sec-btn" :title="showKey ? '隐藏密钥' : '显示密钥'" :aria-label="showKey ? '隐藏密钥' : '显示密钥'" @click="showKey = !showKey">
            <svg v-if="!showKey" viewBox="0 0 16 16" width="14" height="14">
              <path d="M1.5 8S3.8 3.8 8 3.8 14.5 8 14.5 8 12.2 12.2 8 12.2 1.5 8 1.5 8z" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round" />
              <circle cx="8" cy="8" r="1.8" fill="none" stroke="currentColor" stroke-width="1.3" />
            </svg>
            <svg v-else viewBox="0 0 16 16" width="14" height="14">
              <path d="M2.5 2.5l11 11" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
              <path d="M1.5 8S3.8 3.8 8 3.8c1.2 0 2.2.3 3.1.8M14.5 8s-.8 1.4-2.1 2.6M6 6.2A3.4 3.4 0 0 0 8 11.8a3.3 3.3 0 0 0 1.9-.6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" />
            </svg>
          </button>
        </div>
        <div class="hint">每个员工一个，用来向服务器证明身份；丢了找主管重新要</div>
      </div>
    </div>

    <!-- ============ 模板 ============ -->
    <div v-else-if="tab === 'templates'" class="card">
      <input ref="fileInput" type="file" accept=".psd" multiple style="display: none" @change="onPickFiles" />
      <div
        class="dropzone"
        :class="{ drag: dragging }"
        role="button"
        tabindex="0"
        @click="pickFiles"
        @keydown.enter="pickFiles"
        @keydown.space.prevent="pickFiles"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDrop"
      >
        <span class="dz-ico">
          <svg viewBox="0 0 16 16" width="16" height="16">
            <path d="M8 11V3.8M4.8 7L8 3.2 11.2 7" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" />
            <path d="M2.6 11.4v1.2a1.4 1.4 0 0 0 1.4 1.4h8a1.4 1.4 0 0 0 1.4-1.4v-1.2" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
          </svg>
        </span>
        <span>{{ uploading ? '上传中…' : '点击选择 PSD 文件，或拖拽到此处' }}</span>
      </div>

      <div v-if="templateCount" class="tpl-list" style="margin-top: 9px">
        <div v-for="t in templates" :key="t.id" class="tpl-row">
          <span class="tpl-name" :title="t.name">{{ t.name }}</span>
          <span class="tpl-size">{{ sizeText(t.size) }}</span>
          <button
            class="tpl-del"
            :title="`删除模板「${t.name}」`"
            :aria-label="`删除模板 ${t.name}`"
            @click="removeTemplate(t)"
          >
            <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">
              <path d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6" />
            </svg>
          </button>
        </div>
      </div>
      <div v-else class="hint" style="margin-top: 8px">还没有模板，先上传一个 PSD 吧</div>

      <div class="tpl-foot">
        <button type="button" class="link" @click="openDir">打开模板目录</button>
      </div>
    </div>

    <!-- ============ 生成 ============ -->
    <div v-else-if="tab === 'generate'" class="card">
      <div class="field">
        <label>JPG 质量（1-12）</label>
        <div class="range-row">
          <input v-model.number="form.jpg_quality" type="range" min="1" max="12" step="1" />
          <span class="range-val">{{ form.jpg_quality }}</span>
        </div>
        <div class="hint">数字越大越清晰、文件也越大；10 以上肉眼差别不大</div>
      </div>
      <div class="field">
        <label>图片命名</label>
        <div class="chip-row">
          <button
            v-for="opt in FILENAME_OPTIONS"
            :key="opt.value"
            class="range-chip"
            :class="{ active: form.filename_rule === opt.value }"
            @click="form.filename_rule = opt.value"
          >
            {{ opt.label }}
          </button>
        </div>
        <div class="hint">例如 <span class="mono">{{ filenameSample }}</span>；同名图片自动加 (2)、(3)，不覆盖已生成的图</div>
      </div>
      <div class="field">
        <label>生成后清空表单</label>
        <label class="switch">
          <input v-model="form.auto_clear" type="checkbox" />
          <span class="sw" />
          <span>{{ form.auto_clear ? '开' : '关' }}</span>
        </label>
        <div class="hint">开：生成成功后自动清空作者和标题，方便连续录入；关：保留内容便于修改重试</div>
      </div>
      <div class="field">
        <label>合作伙伴名单（一行一个）</label>
        <textarea v-model="partnersText" rows="4" placeholder="华东出版社&#10;南方文化" />
        <div class="hint">录用页的合作伙伴从这里选，按需要自己增删，保存后生效</div>
      </div>
    </div>

    <!-- ============ 窗口 ============ -->
    <!-- 主题不在这儿：主窗头部有个一键切换的按钮，同一个开关不该有两个入口 -->
    <div v-else-if="tab === 'window'" class="card">
      <div class="field">
        <label>主窗置顶</label>
        <label class="switch">
          <input v-model="form.always_on_top" type="checkbox" />
          <span class="sw" />
          <span>{{ form.always_on_top ? '开' : '关' }}</span>
        </label>
        <div class="hint">开：主窗浮在其他应用之上，边看资料边录入；打开记录/设置窗时主窗自动让出</div>
      </div>
      <div class="field">
        <label>拖到边缘自动贴边</label>
        <label class="switch">
          <input v-model="form.auto_dock" type="checkbox" />
          <span class="sw" />
          <span>{{ form.auto_dock ? '开' : '关' }}</span>
        </label>
        <div class="hint">开：把主窗拖到任一屏缘 24px 内松手即贴边，<b>贴哪边看离哪边近</b>，不用选；关：只点主窗上的贴边按钮生效</div>
      </div>
      <div class="field">
        <label>关闭主窗时</label>
        <label class="switch">
          <input v-model="form.close_to_tray" type="checkbox" />
          <span class="sw" />
          <span>{{ form.close_to_tray ? '隐藏到托盘' : '直接退出' }}</span>
        </label>
        <div class="hint">隐藏到托盘：点关闭只是藏起来，从托盘菜单退出，防误关；直接退出：关闭即结束程序</div>
      </div>
    </div>

    <!-- ============ 高级 ============ -->
    <div v-else class="card">
      <div class="field">
        <label>Photoshop 路径</label>
        <input v-model="form.ps_path" type="text" placeholder="留空＝自动识别（推荐）" :style="psPathOk ? '' : 'border-color: var(--err)'" />
        <div v-if="!psPathOk" class="hint" style="color: var(--err)">不是完整路径，应以盘符（如 C:\）开头</div>
        <div v-else-if="psShown" class="ps-detect" :class="{ bad: !psShown.found }">
          <div class="pd-line">
            <svg v-if="psShown.found" class="pd-ico ok" viewBox="0 0 16 16" width="12" height="12">
              <circle cx="8" cy="8" r="6.4" fill="none" stroke="currentColor" stroke-width="1.4" />
              <path d="M5.2 8.3l2 2 3.6-4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            <svg v-else class="pd-ico no" viewBox="0 0 16 16" width="12" height="12">
              <path d="M8 1.6l6.4 11.2H1.6z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" />
              <path d="M8 6v3.2" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" />
              <circle cx="8" cy="11.4" r="0.85" fill="currentColor" />
            </svg>
            <b>{{ psShown.found ? '已自动识别' : '未识别到 Photoshop' }}</b>
            <!-- 来源和版本并到状态行：高级标签本来就快装不下了，能省一行是一行 -->
            <span v-if="psShown.found" class="pd-ver">
              <template v-if="psShown.version">版本 {{ psShown.version }} · </template>{{ psShown.source_label }}
            </span>
            <button
              class="range-chip"
              :disabled="detecting"
              :title="psShown.found ? '换版本或重装 Photoshop 后点这里' : '装好 Photoshop 后再试一次'"
              @click="redetect"
            >
              {{ detecting ? '识别中…' : '重新识别' }}
            </button>
          </div>
          <div v-if="psShown.found" class="pd-path" :title="psShown.path">{{ psShown.path }}</div>
          <!-- 识别不到时才需要指路：认出来了还唠叨"任意版本都能认"是噪音 -->
          <div v-else class="hint">请先安装 Photoshop；装完仍识别不到再手动填上面的路径</div>
          <div v-if="psShown.override_missing" class="hint" style="color: var(--err)">
            你手动填写的路径不存在，当前用的是自动识别结果
          </div>
        </div>
        <div v-else class="hint">正在识别本机的 Photoshop…</div>
      </div>
      <div class="field">
        <label>输出目录</label>
        <input v-model="form.output_dir" type="text" placeholder="D:\期刊录用" :style="outputDirOk ? '' : 'border-color: var(--err)'" />
        <div v-if="!outputDirOk" class="hint" style="color: var(--err)">不是完整路径，应以盘符（如 D:\）开头；相对路径会导致导出失败</div>
        <div v-else class="hint">生成的成品图片保存在这里，修改后对下次生成生效</div>
      </div>
      <div class="field">
        <label>模板目录</label>
        <input v-model="form.template_dir" type="text" placeholder="留空则用默认目录" :style="templateDirOk ? '' : 'border-color: var(--err)'" />
        <div v-if="!templateDirOk" class="hint" style="color: var(--err)">不是完整路径，应以盘符（如 D:\）开头</div>
        <div v-else class="hint">一般不用改；要放共享盘统一更新时再改</div>
      </div>
      <div class="row">
        <div class="field">
          <label>作者占位文字</label>
          <input v-model="form.author_placeholder" type="text" placeholder="{{ '{{作者}}' }}" />
        </div>
        <div class="field">
          <label>标题占位文字</label>
          <input v-model="form.title_placeholder" type="text" placeholder="{{ '{{标题}}' }}" />
        </div>
      </div>
      <div class="hint">PS 模板里代表作者/标题的那两段文字，生成时会自动替换</div>

      <div class="diag-row">
        <span class="diag-ver">客户端 v{{ appVersion }} · 本地服务 v{{ diag?.version ?? '—' }}</span>
        <span class="spacer" />
        <a class="link" @click="copyDiag">复制诊断信息</a>
        <a class="link" @click="openLogs">打开日志</a>
      </div>
    </div>

    <!-- 保存栏：只有改过东西才出现。常驻一条 53px 的空栏等于天天提醒
         "你还没改"，改完它自己滑走，比一句"已保存"更有说服力。 -->
    <div v-if="dirty" class="save-bar">
      <span class="sb-hint">
        <span class="sb-dot" />
        有未保存的改动
        <span style="opacity: 0.75">· <kbd>Ctrl</kbd> <kbd>S</kbd></span>
      </span>
      <button class="btn sm" style="width: auto; padding: 7px 16px" :disabled="saving" @click="save">
        {{ saving ? '保存中…' : '保存设置' }}
      </button>
    </div>
  </div>
</template>
