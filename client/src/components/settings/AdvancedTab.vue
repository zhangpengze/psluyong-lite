<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { api, type ClientConfig, type PsInfo } from '../../api/sidecar'
import { useToast } from '../../composables/useToast'
import { copyText } from '../../utils/clipboard'
import { isWinAbsPath } from '../../utils/validate'

/**
 * 设置 · 高级分组：Photoshop 路径（含重新识别）、输出/模板目录、
 * 占位文字、诊断信息。
 */
const props = defineProps<{
  form: ClientConfig
  /** 本机 Photoshop 的识别结果（由外壳的 /status 轮询带下来，不重复请求） */
  ps: PsInfo | null
  /** 外壳挂载时拉一次的诊断信息 */
  diag: { version: string; log_dir: string; machine: string; platform: string } | null
  templateCount: number
}>()

const { toast } = useToast()
const appVersion = __APP_VERSION__

function flash(text: string, type: 'success' | 'warning' | 'error' = 'success') {
  toast(text, type)
}

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

/** 路径字段必须是完整路径（规则在 utils/validate，保存前的拦截同用一份） */
const outputDirOk = computed(() => isWinAbsPath(props.form.output_dir))
const templateDirOk = computed(() => isWinAbsPath(props.form.template_dir))
const psPathOk = computed(() => isWinAbsPath(props.form.ps_path))

/** 员工报障用的诊断文本：一次复制，主管不用再来回问环境 */
async function copyDiag() {
  const lines = [
    `期刊录用助手 客户端 v${appVersion} / 本地服务 v${props.diag?.version ?? '—'}`,
    `机器：${props.diag?.machine || '—'}`,
    `系统：${props.diag?.platform || '—'}`,
    `姓名：${props.form.employee_name || '(未设置)'}`,
    `服务器：${props.form.server_url || '(未设置)'}`,
    `模板：${props.templateCount} 个`,
    `日志：${props.diag?.log_dir || '—'}`,
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

/**
 * 导出诊断包：把日志、配置（已抹掉 Key）、最近几次生成的现场打成一个 zip。
 * 「复制诊断信息」只有 7 行，遇到"配置全空/界面全 500"时员工给不出任何有用东西。
 */
const diagBusy = ref(false)
async function saveDiagBundle() {
  diagBusy.value = true
  try {
    const res = await api.saveDiagnostics()
    if (res.ok) flash('诊断包已导出到 diagnostics 文件夹，请把它发给主管', 'success')
    else flash(res.error || '生成诊断包失败', 'error')
  } catch (e) {
    flash(e instanceof Error ? e.message : '生成诊断包失败', 'error')
  } finally {
    diagBusy.value = false
  }
}
</script>

<template>
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
    <a class="link" @click="diagBusy ? undefined : saveDiagBundle">{{ diagBusy ? '正在打包…' : '导出诊断包' }}</a>
    <a class="link" @click="openLogs">打开日志</a>
  </div>
</template>
