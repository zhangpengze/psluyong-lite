<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ClientConfig } from './api/sidecar'
import {
  broadcastEvent,
  forceQuit,
  hasTauri,
  hideMainWindow,
  listenAppEvent,
  listenCloseWhileBusy,
  setGenerating,
  setTrayTooltip,
} from './api/windowCtl'
import { useConfirm } from './composables/useConfirm'
import { useHashRoute } from './composables/useHashRoute'
import { useMainHeight } from './composables/useMainHeight'
import { useSidecarStatus } from './composables/useSidecarStatus'
import { useTheme } from './composables/useTheme'
import { useWindowControls } from './composables/useWindowControls'
import AppHeader from './components/AppHeader.vue'
import RecordForm from './components/RecordForm.vue'
import HistoryPanel from './components/HistoryPanel.vue'
import SettingsPanel from './components/SettingsPanel.vue'
import ToastHost from './components/ToastHost.vue'
import ConfirmDialog from './components/ConfirmDialog.vue'

const { confirm } = useConfirm()

// ==================== 三块外壳状态各自成器 ====================
// 路由（hash 分发三窗口）、主题、sidecar 数据、窗口控制、主窗高度自适应
// 都在 composables/ 里，这一层只负责把它们接起来、编排启动流程。
const route = useHashRoute()
const { theme, applyTheme, toggleTheme } = useTheme()

const booting = ref(true)
// 生成成功后让记录面板重新拉一次（同窗口内），跨窗口走 record-added 事件
const historyVersion = ref(0)
// 录用页实时上报的生成中状态
const formBusy = ref(false)

const {
  cfg, templates, partners, serverOk, comAvailable, ps,
  pending, deadCount, pendingBlocked, blockedHint, loadError, serverBusy, todayCount,
  refreshStatus, waitForSidecar, refreshTemplates, refreshDicts,
  flushQueue, retryDead, refreshToday, startPolling,
} = useSidecarStatus({ theme, applyTheme })

// ==================== 生成中状态（关窗拦截 + 贴边不缩回用）====================
// formBusy：录用页实时上报；serverBusy：sidecar 权威值（切窗口后仍然准确）
const generating = computed(() => formBusy.value || serverBusy.value)
watch(generating, (v) => {
  if (hasTauri()) setGenerating(v).catch(() => {})
})

const {
  hasWindowCtl, pinned, docked, togglePin, toggleDock, openAux,
  applyWindowPrefs, applyWindowConfigDiff, startListeners,
} = useWindowControls({ cfg, route })

useMainHeight({ route, booting })

/** 配置完成度：姓名 / API Key / 服务器地址 三项。
 *  short 是按钮上用的短词（按钮只有 340px 宽，塞不下整句）；
 *  label 是悬停提示里的完整说法。 */
const setupItems = computed(() => [
  { label: '填写你的姓名', short: '姓名', done: !!cfg.value?.employee_name },
  { label: '填写服务器地址', short: '服务器地址', done: !!cfg.value?.server_url },
  { label: '填写 API Key（向主管索取）', short: 'API Key', done: !!cfg.value?.api_key },
])
/** 只列还没做的项，已完成的不占地方；短词印在主按钮上，完整说法挂按钮 title */
const missingItems = computed(() => setupItems.value.filter((i) => !i.done))

const deadHint = computed(
  () =>
    `有 ${deadCount.value} 条记录多次补传都没成功，已移到本地 dead 目录留档。` +
    '确认服务器恢复正常后点「重试死信」再试一次。',
)

function onConfigSaved(next: ClientConfig) {
  const prev = cfg.value
  cfg.value = next
  applyTheme(next.theme === 'light' ? 'light' : 'dark')
  refreshTemplates()
  refreshDicts()
  refreshStatus()
  // 窗口类偏好只有主窗能应用：命令在 Rust 里固定操作 main 标签
  applyWindowConfigDiff(prev, next)
  // 其他窗口（记录窗）同步主题与状态
  broadcastEvent('config-changed', { theme: next.theme }).catch(() => {})
}

function onExported() {
  historyVersion.value += 1
  refreshStatus()
  refreshToday()
  // 记录窗（可能开着）收到后自动刷新，员工不用手动点
  broadcastEvent('record-added', {}).catch(() => {})
}

function onHistoryStats(stats: { total: number; today: number }) {
  todayCount.value = stats.today
}

let started = false

/** 本地服务就绪后才做的事：模板/名单、补传、窗口偏好、轮询、事件 */
async function startAfterReady() {
  if (started) return
  started = true

  await Promise.all([refreshTemplates(), refreshDicts()])
  flushQueue()
  refreshToday()

  // 窗口三件套：恢复上次的位置/置顶/贴边，再挂贴边回写、位置记忆、自动贴边
  if (cfg.value) applyWindowPrefs(cfg.value)
  startListeners()

  // 生成中关窗拦截：Rust 拦下并通知到这里，确认后才真的退出
  listenCloseWhileBusy(async () => {
    const ok = await confirm({
      title: '正在生成中',
      message: '录用图还在生成（或等待 PS 响应）。现在关闭会中断本次生成，Photoshop 里可能残留已打开的模板。',
      danger: true,
      confirmText: '仍然退出',
      cancelText: '继续等待',
    })
    if (ok) await forceQuit().catch(() => {})
  })

  // 跨窗口同步：设置窗保存后这里刷新主题；主窗生成完成后记录窗刷新
  listenAppEvent('config-changed', () => {
    refreshStatus(true)
  })
  listenAppEvent('record-added', () => {
    historyVersion.value += 1
    refreshStatus(true)
  })

  // 状态轮询：服务器恢复后自动补传，气泡数字跟着走
  startPolling()
}

/** 错误卡片上的「重试连接」：重新等 sidecar，成功后再走一遍启动流程 */
async function retryBoot() {
  booting.value = true
  loadError.value = ''
  const ready = await waitForSidecar()
  booting.value = false
  if (ready) await startAfterReady()
}

/** Esc 隐藏主窗：迷你工具的快速唤出/隐藏，像输入法一样 */
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && route.value === 'main' && hasTauri()) {
    hideMainWindow().catch(() => {})
  }
}

/** 托盘气泡：在线状态 + 今日进度 + 待补传，不开窗也知道全局 */
watch([serverOk, pending, todayCount], () => {
  if (!hasTauri()) return
  const state = serverOk.value ? '在线' : '离线'
  const text = `期刊录用助手 · ${state} · 今日 ${todayCount.value} · 待补传 ${pending.value}`
  setTrayTooltip(text).catch(() => {})
})

onMounted(async () => {
  // hash 驱动路由由 useHashRoute 监听；这里只负责启动链路
  window.addEventListener('keydown', onKeydown)

  booting.value = true
  const ready = await waitForSidecar()
  booting.value = false
  if (!ready) return // 停在错误卡片上，等员工点「重试连接」

  await startAfterReady()
})

onUnmounted(() => window.removeEventListener('keydown', onKeydown))
</script>

<template>
  <div class="wallpaper" aria-hidden="true" />

  <div class="app">
    <!-- 主窗头部：logo + 记录/设置入口 + 窗口控件 + 服务器状态（单行） -->
    <AppHeader
      v-if="route === 'main'"
      :server-ok="serverOk"
      :pinned="pinned"
      :docked="docked"
      :theme="theme"
      :has-window-ctl="hasWindowCtl"
      @open-aux="openAux"
      @toggle-pin="togglePin"
      @toggle-dock="toggleDock"
      @toggle-theme="toggleTheme"
    />

    <main ref="bodyRef" class="body" :class="{ bare: route !== 'main' }">
      <div v-if="booting" class="empty">
        <span class="e-ico">
          <svg viewBox="0 0 16 16" width="18" height="18">
            <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.3" stroke-dasharray="28 12" />
          </svg>
        </span>
        正在启动…
      </div>

      <template v-else>
        <div v-if="loadError" class="result fail">
          <div class="head"><span class="ico">!</span>{{ loadError }}</div>
          <div>客户端可能还在启动本地服务，点下面按钮再试一次；仍不行请重启软件。</div>
          <button class="btn ghost sm" style="margin-top: 9px" @click="retryBoot">重试连接</button>
        </div>

        <!-- 配置没配完时不再单独占一条横幅：那句话搬到主按钮上
             （"还差 2 步：姓名 · API Key"，点它直接去设置）。
             首启引导本来就该长在"现在该做什么"上，而不是常驻一块地方。 -->

        <div v-if="!comAvailable && route === 'main'" class="result warn">
          <div class="head"><span class="ico">!</span>本机缺少 PS 自动化组件</div>
          <div>未检测到 pywin32，无法调用 Photoshop。请安装依赖后重启客户端。</div>
        </div>

        <!-- 装没装 PS 是运行时才知道的事：识别不到就在这儿说一声，
             别等员工点完生成才弹一个看不懂的 COM 错误。
             COM 组件缺失（pywin32）优先报，那条更根本。 -->
        <div v-else-if="ps && !ps.found && route === 'main'" class="result warn">
          <div class="head"><span class="ico">!</span>未识别到本机安装的 Photoshop</div>
          <div>
            自动识别没找到 Photoshop（任意版本、任意盘符都会找）。请先安装，
            或到设置里手动指定路径。
          </div>
          <button class="btn ghost sm" style="margin-top: 9px" @click="openAux('settings')">去设置</button>
        </div>

        <RecordForm
          v-if="route === 'main'"
          :config="cfg"
          :templates="templates"
          :partners="partners"
          :ready="!!cfg?.employee_name && !!cfg?.api_key"
          :missing="missingItems.map((i) => i.short)"
          :missing-detail="missingItems.map((i) => i.label).join('、')"
          @exported="onExported"
          @busy-change="formBusy = $event"
          @need-settings="openAux('settings')"
        />

        <HistoryPanel
          v-else-if="route === 'history'"
          :version="historyVersion"
          :config="cfg"
          :templates="templates"
          :partners="partners"
          @stats="onHistoryStats"
        />

        <SettingsPanel
          v-else
          :config="cfg"
          :server-ok="serverOk"
          :ps="ps"
          :templates="templates"
          @saved="onConfigSaved"
          @templates-changed="refreshTemplates"
        />
      </template>
    </main>

    <!-- 主窗底栏：无事可报时整体隐藏（离线/待补传/死信才出现），姓名悬停可见 -->
    <footer
      v-if="route === 'main' && (pending > 0 || deadCount > 0 || !serverOk)"
      class="statusbar"
    >
      <span class="me" :title="cfg?.employee_name || ''">{{ cfg?.employee_name || '未设置姓名' }}</span>
      <span v-if="!serverOk">服务器离线</span>
      <span v-if="pending > 0">待补传 {{ pending }} 条</span>
      <!-- 配置受阻（Key/地址错）单独说清楚：旧版这些记录会被静默删掉，
           员工只看到"待补传"归零以为成功了。现在记录留着，原因是配置。 -->
      <span v-if="pendingBlocked > 0" class="blocked" :title="blockedHint">
        其中 {{ pendingBlocked }} 条因配置受阻
      </span>
      <span v-if="deadCount > 0" class="dead" :title="deadHint">死信 {{ deadCount }} 条</span>
      <div class="spacer" />
      <a v-if="pending > 0" class="link" @click="flushQueue">立即补传</a>
      <a v-if="deadCount > 0" class="dead-link" @click="retryDead">重试死信</a>
    </footer>
  </div>

  <ToastHost />
  <ConfirmDialog />
</template>
