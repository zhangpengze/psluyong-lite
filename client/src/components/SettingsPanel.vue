<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, type ClientConfig, type TemplateItem } from '../api/sidecar'
import { useConfirm } from '../composables/useConfirm'
import { useToast } from '../composables/useToast'
import { copyText } from '../utils/clipboard'

const props = defineProps<{
  config: ClientConfig | null
  serverOk: boolean
  templates: TemplateItem[]
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
  dock_side: '',
  auto_dock: true,
  close_to_tray: true,
  win_x: -1,
  win_y: -1,
  history_win_x: -1,
  history_win_y: -1,
  settings_win_x: -1,
  settings_win_y: -1,
  theme: 'dark',
})

/** 名单用「一行一个」的文本框维护，比一堆输入框更适合批量粘贴 */
const partnersText = ref('')

const saving = ref(false)
const uploading = ref(false)
const testing = ref(false)
const showKey = ref(false)
const dragging = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

const templateCount = computed(() => props.templates.length)
const nameOk = computed(() => form.employee_name.trim().length > 0)
const urlOk = computed(() => form.server_url.trim().startsWith('http'))

/** 图片命名选项：示例用固定的作者/标题，让员工一眼看出区别 */
const FILENAME_OPTIONS = [
  { value: 'author', label: '作者姓名', sample: '李四.jpg' },
  { value: 'author_title', label: '作者 + 标题', sample: '李四_基于深度学习的图像分割方法研究.jpg' },
] as const

/** 贴边方向：空 = 不贴边 */
const DOCK_OPTIONS = [
  { value: '', label: '不贴边', sample: '普通窗口，随处放' },
  { value: 'left', label: '贴左缘', sample: '藏到屏幕左边，露 6px 窄条' },
  { value: 'right', label: '贴右缘', sample: '藏到屏幕右边，露 6px 窄条' },
  { value: 'top', label: '贴上缘', sample: '藏到屏幕顶部，露 6px 窄条' },
] as const

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
      form.dock_side !== props.config.dock_side ||
      form.auto_dock !== props.config.auto_dock ||
      form.close_to_tray !== props.config.close_to_tray ||
      form.theme !== props.config.theme ||
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
  },
  { immediate: true },
)

function flash(text: string, type: 'success' | 'warning' | 'error' = 'success') {
  toast(text, type)
}

async function save() {
  if (!nameOk.value) {
    flash('请填写你的姓名，主管后台按姓名统计', 'error')
    return
  }
  if (!urlOk.value) {
    flash('服务器地址应以 http:// 开头', 'error')
    return
  }
  if (!outputDirOk.value) {
    flash('输出目录不是完整路径，应以盘符（如 D:\\）开头', 'error')
    return
  }
  if (!templateDirOk.value) {
    flash('模板目录不是完整路径，应以盘符（如 D:\\）开头', 'error')
    return
  }
  if (!psPathOk.value) {
    flash('Photoshop 路径不是完整路径，应以盘符（如 C:\\）开头', 'error')
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
      dock_side: ['left', 'right', 'top'].includes(form.dock_side) ? form.dock_side : '',
      auto_dock: form.auto_dock,
      close_to_tray: form.close_to_tray,
      theme: form.theme === 'light' ? 'light' : 'dark',
    })
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
  <div class="stack">
    <div class="card">
      <h3>账号</h3>
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
      </div>
    </div>

    <div class="card">
      <h3>PS 模板<span class="sub">{{ templateCount }} 个</span></h3>

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

      <div class="btnrow" style="margin-top: 8px">
        <button class="btn ghost" @click="openDir">打开模板目录</button>
      </div>

      <div v-if="templateCount" class="tpl-list" style="margin-top: 9px">
        <div v-for="t in templates" :key="t.id" class="tpl-row">
          <span class="tpl-name" :title="t.name">{{ t.name }}</span>
          <span class="tpl-size">{{ sizeText(t.size) }}</span>
          <button class="btn mini" @click="removeTemplate(t)">删除</button>
        </div>
      </div>
      <div v-else class="hint" style="margin-top: 8px">还没有模板，先上传一个 PSD 吧</div>
    </div>

    <div class="card">
      <h3>合作伙伴</h3>
      <div class="field">
        <label>名单（一行一个）</label>
        <textarea v-model="partnersText" rows="4" placeholder="华东出版社&#10;南方文化" />
        <div class="hint">录用页的合作伙伴从这里选，按需要自己增删，保存后生效</div>
      </div>
    </div>

    <div class="card">
      <h3>生成设置</h3>
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
        <div class="opt-list">
          <label
            v-for="opt in FILENAME_OPTIONS"
            :key="opt.value"
            class="opt-row"
            :class="{ active: form.filename_rule === opt.value }"
          >
            <input v-model="form.filename_rule" type="radio" name="filename-rule" :value="opt.value" />
            <span class="opt-text">
              <span class="opt-name">{{ opt.label }}</span>
              <span class="opt-sample">{{ opt.sample }}</span>
            </span>
          </label>
        </div>
        <div class="hint">同名图片已存在时自动加 (2)、(3)，不会覆盖已生成的图</div>
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
    </div>

    <div class="card">
      <h3>外观与窗口</h3>
      <div class="field">
        <label>主题</label>
        <div class="range-row">
          <button class="range-chip" :class="{ active: form.theme !== 'light' }" @click="form.theme = 'dark'">深色</button>
          <button class="range-chip" :class="{ active: form.theme === 'light' }" @click="form.theme = 'light'">浅色</button>
        </div>
        <div class="hint">偏好存在本机配置里，换机器不丢</div>
      </div>
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
        <label>贴边方向</label>
        <div class="opt-list">
          <label
            v-for="opt in DOCK_OPTIONS"
            :key="opt.value"
            class="opt-row"
            :class="{ active: form.dock_side === opt.value }"
          >
            <input v-model="form.dock_side" type="radio" name="dock-side" :value="opt.value" />
            <span class="opt-text">
              <span class="opt-name">{{ opt.label }}</span>
              <span class="opt-sample">{{ opt.sample }}</span>
            </span>
          </label>
        </div>
        <div class="hint">贴边后窗口大部分移出屏幕，只露 6px 窄条，鼠标碰到即滑出</div>
      </div>
      <div class="field">
        <label>拖到边缘自动贴边</label>
        <label class="switch">
          <input v-model="form.auto_dock" type="checkbox" />
          <span class="sw" />
          <span>{{ form.auto_dock ? '开' : '关' }}</span>
        </label>
        <div class="hint">开：把主窗拖到任一屏缘 24px 内松手即贴边；关：只点主窗上的贴边按钮生效</div>
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

    <details class="adv">
      <summary>
        高级设置
        <span class="adv-arrow">
          <svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M4 6l4 4 4-4" />
          </svg>
        </span>
      </summary>
      <div class="adv-body">
        <div class="field">
          <label>Photoshop 路径</label>
          <input v-model="form.ps_path" type="text" placeholder="留空则自动连接正在运行的 Photoshop" :style="psPathOk ? '' : 'border-color: var(--err)'" />
          <div v-if="!psPathOk" class="hint" style="color: var(--err)">不是完整路径，应以盘符（如 C:\）开头</div>
          <div v-else class="hint">建议先手动打开 Photoshop 再生成，速度更快</div>
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
      </div>
    </details>

    <!-- 版本与诊断：员工报障时一键复制环境信息（放在保存栏之前，避免被 sticky 底栏挡住） -->
    <div class="diag-row">
      <span class="diag-ver">客户端 v{{ appVersion }} · 本地服务 v{{ diag?.version ?? '—' }}</span>
      <span class="spacer" />
      <a class="link" @click="copyDiag">复制诊断信息</a>
      <a class="link" @click="openLogs">打开日志</a>
    </div>

    <div class="save-bar">
      <span class="sb-hint">
        <span class="sb-dot" :class="{ saved: !dirty }" />
        {{ dirty ? '有未保存的改动' : '所有改动已保存' }}
        <span style="opacity: 0.75">· <kbd>Ctrl</kbd> <kbd>S</kbd></span>
      </span>
      <button class="btn sm" style="width: auto; padding: 7px 16px" :disabled="saving || !dirty" @click="save">
        {{ saving ? '保存中…' : '保存设置' }}
      </button>
    </div>
  </div>
</template>
