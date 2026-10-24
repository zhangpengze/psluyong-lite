<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { api, type ClientConfig, type PsInfo, type TemplateItem } from '../api/sidecar'
import { useToast } from '../composables/useToast'
import { isHttpUrl, isWinAbsPath } from '../utils/validate'
import AccountTab from './settings/AccountTab.vue'
import TemplatesTab from './settings/TemplatesTab.vue'
import GenerateTab from './settings/GenerateTab.vue'
import WindowTab from './settings/WindowTab.vue'
import AdvancedTab from './settings/AdvancedTab.vue'

const props = defineProps<{
  config: ClientConfig | null
  serverOk: boolean
  templates: TemplateItem[]
  /** 本机 Photoshop 的识别结果（由外壳的 /status 轮询带下来，不重复请求） */
  ps: PsInfo | null
}>()
const emit = defineEmits<{ saved: [ClientConfig]; 'templates-changed': [] }>()

const { toast } = useToast()

function flash(text: string, type: 'success' | 'warning' | 'error' = 'success') {
  toast(text, type)
}

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

const templateCount = computed(() => props.templates.length)
const nameOk = computed(() => form.employee_name.trim().length > 0)
const urlOk = computed(() => isHttpUrl(form.server_url))

/** 路径字段的完整路径判定与保存前拦截共用 utils/validate 的同一份规则 */
const outputDirOk = computed(() => isWinAbsPath(form.output_dir))
const templateDirOk = computed(() => isWinAbsPath(form.template_dir))
const psPathOk = computed(() => isWinAbsPath(form.ps_path))

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

// ---------- 版本与诊断（诊断挂在高级分组里，但数据在挂载时就拉好） ----------
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

function onKeydown(e: KeyboardEvent) {
  if (e.ctrlKey && e.key.toLowerCase() === 's') {
    e.preventDefault()
    save()
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

    <!-- 五个分组各拆一个子组件：卡片壳与切换留在这里 -->
    <div v-if="tab === 'account'" class="card">
      <AccountTab :form="form" :server-ok="serverOk" />
    </div>

    <div v-else-if="tab === 'templates'" class="card">
      <TemplatesTab :templates="templates" @templates-changed="emit('templates-changed')" />
    </div>

    <div v-else-if="tab === 'generate'" class="card">
      <GenerateTab v-model:partners-text="partnersText" :form="form" />
    </div>

    <!-- 主题不在这儿：主窗头部有个一键切换的按钮，同一个开关不该有两个入口 -->
    <div v-else-if="tab === 'window'" class="card">
      <WindowTab :form="form" />
    </div>

    <div v-else class="card">
      <AdvancedTab :form="form" :ps="ps" :diag="diag" :template-count="templateCount" />
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
