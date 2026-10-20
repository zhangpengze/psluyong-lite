<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { api, type ClientConfig, type TemplateItem } from './api/sidecar'
import {
  dockWindow,
  getWindowRect,
  hasTauri,
  listenDockChanged,
  restoreWindowRect,
  setAlwaysOnTop,
  undockWindow,
} from './api/windowCtl'
import RecordForm from './components/RecordForm.vue'
import HistoryPanel from './components/HistoryPanel.vue'
import SettingsPanel from './components/SettingsPanel.vue'

type Tab = 'record' | 'history' | 'settings'

const TABS: Tab[] = ['record', 'history', 'settings']

function tabFromHash(): Tab {
  const raw = window.location.hash.replace('#', '')
  return (TABS as string[]).includes(raw) ? (raw as Tab) : 'record'
}

const tab = ref<Tab>(tabFromHash())
const cfg = ref<ClientConfig | null>(null)
const templates = ref<TemplateItem[]>([])
const partners = ref<string[]>([])
const serverOk = ref(false)
const comAvailable = ref(false)
const pending = ref(0)
const deadCount = ref(0)
const booting = ref(true)
const loadError = ref('')
// 生成成功后让历史面板重新拉一次
const historyVersion = ref(0)

const showSetupHint = computed(
  () => !cfg.value?.employee_name || !cfg.value?.api_key || !cfg.value?.server_url,
)

const tabTitle = computed(
  () => ({ record: '期刊录用', history: '我的记录', settings: '设置' })[tab.value],
)

const deadHint = computed(
  () =>
    `有 ${deadCount.value} 条记录多次补传都没成功，已移到本地 dead 目录留档。` +
    '确认服务器恢复正常后点「重试死信」再试一次。',
)

async function refreshStatus() {
  try {
    const s = await api.status()
    cfg.value = s.config
    serverOk.value = s.server_reachable
    comAvailable.value = s.com_available
    pending.value = s.pending
    deadCount.value = s.dead ?? 0
    loadError.value = ''
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '本地服务连接失败'
  }
}

async function refreshTemplates() {
  try {
    const res = await api.templates()
    if (res.ok) templates.value = res.items
    else loadError.value = res.error || '获取模板失败'
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '获取模板失败'
  }
}

async function refreshDicts() {
  try {
    const res = await api.dicts()
    partners.value = res.grouped?.partner ?? []
  } catch {
    partners.value = []
  }
}

async function flushQueue() {
  try {
    const res = await api.flushQueue()
    pending.value = res.remaining
    deadCount.value = res.dead ?? deadCount.value
    if (res.sent > 0) refreshTemplates()
  } catch {
    /* 补传失败不打扰员工 */
  }
}

/** 死信是多次补传仍失败的记录，只有人确认服务端正常了才该重试 */
async function retryDead() {
  if (!window.confirm(`把 ${deadCount.value} 条死信重新排入补传队列？`)) return
  try {
    const res = await api.retryDead()
    pending.value = res.remaining
    deadCount.value = res.dead
    if (res.sent > 0) refreshTemplates()
    if (res.restored > 0 && res.sent === 0) {
      window.alert(`已重新排队 ${res.restored} 条，但本次补传仍未成功，请检查服务器是否已恢复。`)
    }
  } catch {
    window.alert('重试失败，请稍后再试')
  }
}

function onConfigSaved(next: ClientConfig) {
  cfg.value = next
  refreshTemplates()
  refreshDicts()
  refreshStatus()
}

function onExported() {
  historyVersion.value += 1
  refreshStatus()
}

// ==================== 窗口三件套：置顶 / 贴边 / 位置记忆 ====================
// 浏览器预览里 hasTauri() 为 false，按钮隐藏、方法 no-op
const hasWindowCtl = ref(hasTauri())
const pinned = ref(false)
const docked = ref(false)
let rectTimer: number | undefined

async function togglePin() {
  const next = !pinned.value
  try {
    await setAlwaysOnTop(next)
    pinned.value = next
    await api.saveConfig({ always_on_top: next })
  } catch {
    /* 置顶失败极少见，不打扰员工 */
  }
}

async function toggleDock() {
  try {
    if (docked.value) {
      await undockWindow()
      docked.value = false
      await api.saveConfig({ dock_side: '' })
    } else {
      await dockWindow('right')
      docked.value = true
      await api.saveConfig({ dock_side: 'right' })
    }
  } catch {
    /* 贴边失败保持原状 */
  }
}

/** 定期把窗口位置存进配置，下次启动恢复 */
async function rememberWindowRect() {
  if (!hasTauri() || docked.value) return
  const rect = await getWindowRect()
  if (rect && (rect[0] !== cfg.value?.win_x || rect[1] !== cfg.value?.win_y)) {
    await api.saveConfig({ win_x: rect[0], win_y: rect[1] })
  }
}

async function applyWindowPrefs(c: ClientConfig) {
  if (!hasTauri()) return
  if (c.win_x >= 0) {
    // 先恢复上次的窗口位置；若贴边，dock 命令会再接管横向位置
    await restoreWindowRect(c.win_x, c.win_y).catch(() => {})
  }
  if (c.always_on_top) {
    await setAlwaysOnTop(true).catch(() => {})
    pinned.value = true
  }
  if (c.dock_side === 'left' || c.dock_side === 'right') {
    await dockWindow(c.dock_side).catch(() => {})
    docked.value = true
  }
}

onMounted(async () => {
  // 记到 hash 里：刷新或误关后能停回原来的标签页
  window.addEventListener('hashchange', () => {
    tab.value = tabFromHash()
  })
  watch(tab, (next) => {
    if (window.location.hash !== `#${next}`) window.location.hash = next
  })

  await refreshStatus()
  await Promise.all([refreshTemplates(), refreshDicts()])
  booting.value = false
  flushQueue()

  // 窗口三件套：恢复上次的位置/置顶/贴边
  if (cfg.value) applyWindowPrefs(cfg.value)
  listenDockChanged((side) => {
    docked.value = side !== null
    if (side === null) api.saveConfig({ dock_side: '' }).catch(() => {})
  })
  rectTimer = window.setInterval(rememberWindowRect, 10_000)
})

onUnmounted(() => {
  if (rectTimer) window.clearInterval(rectTimer)
})
</script>

<template>
  <div class="app">
    <div class="topbar">
      <div class="logo">录</div>
      <div>
        <div class="title">{{ tabTitle }}</div>
        <div class="who">{{ cfg?.employee_name || '未设置姓名' }}</div>
      </div>
      <div class="spacer" />
      <template v-if="hasWindowCtl">
        <button class="winbtn" :class="{ active: pinned }" title="置顶窗口" @click="togglePin">顶</button>
        <button class="winbtn" :class="{ active: docked }" title="贴边隐藏" @click="toggleDock">边</button>
      </template>
      <span class="dot" :class="serverOk ? 'on' : 'off'">
        {{ serverOk ? '服务器正常' : '服务器离线' }}
      </span>
    </div>

    <div class="tabs">
      <button :class="{ active: tab === 'record' }" @click="tab = 'record'">录用</button>
      <button :class="{ active: tab === 'history' }" @click="tab = 'history'">记录</button>
      <button :class="{ active: tab === 'settings' }" @click="tab = 'settings'">设置</button>
    </div>

    <div class="body">
      <div v-if="booting" class="empty">正在启动…</div>

      <template v-else>
        <div v-if="loadError" class="result fail">
          <div class="head"><span class="ico">!</span>{{ loadError }}</div>
          <div>请确认后端服务已启动，或在「设置」里检查服务器地址。</div>
        </div>

        <div v-if="showSetupHint" class="result warn">
          <div class="head"><span class="ico">!</span>还差几步才能开始</div>
          <ul>
            <li v-if="!cfg?.server_url">填写服务器地址</li>
            <li v-if="!cfg?.api_key">填写 API Key（向主管索取）</li>
            <li v-if="!cfg?.employee_name">填写你的姓名</li>
          </ul>
          <div style="margin-top: 8px">
            <button class="btn ghost" @click="tab = 'settings'">去设置</button>
          </div>
        </div>

        <div v-if="!comAvailable && tab === 'record'" class="result warn">
          <div class="head"><span class="ico">!</span>本机缺少 PS 自动化组件</div>
          <div>未检测到 pywin32，无法调用 Photoshop。请安装依赖后重启客户端。</div>
        </div>

        <RecordForm
          v-if="tab === 'record'"
          :config="cfg"
          :templates="templates"
          :partners="partners"
          :ready="!!cfg?.employee_name && !!cfg?.api_key"
          @exported="onExported"
          @need-settings="tab = 'settings'"
        />

        <HistoryPanel
          v-else-if="tab === 'history'"
          :version="historyVersion"
          :openable="true"
        />

        <SettingsPanel
          v-else
          :config="cfg"
          :server-ok="serverOk"
          :templates="templates"
          @saved="onConfigSaved"
          @templates-changed="refreshTemplates"
        />
      </template>
    </div>

    <div class="statusbar">
      <span v-if="pending > 0">待补传 {{ pending }} 条</span>
      <span v-else>记录已同步</span>
      <span v-if="deadCount > 0" class="dead" :title="deadHint">
        死信 {{ deadCount }} 条
      </span>
      <div class="spacer" />
      <a v-if="pending > 0" @click="flushQueue">立即补传</a>
      <a v-if="deadCount > 0" class="dead-link" @click="retryDead">重试死信</a>
    </div>
  </div>
</template>
