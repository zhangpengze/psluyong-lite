/**
 * sidecar 状态与数据（composable）
 *
 * 配置 / 模板 / 名单 / 队列 / 今日计数的加载与刷新，60 秒轮询（含断线恢复
 * 自动补传）、死信重试。所有数字以 /status 为权威，界面各处只读这里的状态。
 */

import { onUnmounted, ref, type Ref } from 'vue'
import { api, type ClientConfig, type PsInfo, type TemplateItem } from '../api/sidecar'
import { useConfirm } from './useConfirm'
import { useToast } from './useToast'
import { localDateKey } from '../utils/history'

export interface SidecarStatusOptions {
  /** 主题以服务端配置为准：refreshStatus 拿到新配置后由这里判「变没变」 */
  theme: Ref<'dark' | 'light'>
  applyTheme: (t: 'dark' | 'light') => void
}

export function useSidecarStatus(opts: SidecarStatusOptions) {
  const { confirm } = useConfirm()
  const { toast } = useToast()

  const cfg = ref<ClientConfig | null>(null)
  const templates = ref<TemplateItem[]>([])
  const partners = ref<string[]>([])
  const serverOk = ref(false)
  /** 本机 Photoshop 的自动识别结果（sidecar /status 带下来） */
  const ps = ref<PsInfo | null>(null)
  const comAvailable = ref(false)
  const pending = ref(0)
  const deadCount = ref(0)
  const loadError = ref('')
  /** sidecar 权威的生成中标志（切窗口后仍然准确；录入页实时上报的那个在 App） */
  const serverBusy = ref(false)
  // 记录窗今日生成数（HistoryPanel 回传 / 自己拉，用于托盘气泡）
  const todayCount = ref(0)

  /**
   * @param quiet 静默失败：轮询时不弹大错误条（离线有状态点表达），
   *              只有启动阶段才需要把失败摆到台面上
   */
  async function refreshStatus(quiet = false): Promise<boolean> {
    try {
      const s = await api.status()
      cfg.value = s.config
      serverOk.value = s.server_reachable
      comAvailable.value = s.com_available
      ps.value = s.ps ?? null
      pending.value = s.pending
      deadCount.value = s.dead ?? 0
      serverBusy.value = !!s.busy
      loadError.value = ''
      // 主题以服务端配置为准（换机器/重装不丢偏好）
      const next = s.config.theme === 'light' ? 'light' : 'dark'
      if (next !== opts.theme.value) opts.applyTheme(next)
      return true
    } catch (e) {
      if (!quiet) loadError.value = e instanceof Error ? e.message : '本地服务连接失败'
      return false
    }
  }

  /**
   * sidecar 是 Tauri 启动的子进程，比窗口晚 1~3 秒就绪是常态。
   * 只试一次就报「请重启软件」，员工每次开机都要看一次红条（真实踩坑）。
   */
  async function waitForSidecar(): Promise<boolean> {
    const delays = [0, 400, 800, 1200, 1600, 2000, 2500, 3000]
    for (const d of delays) {
      if (d) await new Promise((r) => setTimeout(r, d))
      if (await refreshStatus(true)) return true
    }
    loadError.value = '本地服务连接失败'
    return false
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
    if (!(await confirm({ title: '重试死信', message: `把 ${deadCount.value} 条死信重新排入补传队列？`, confirmText: '重试' }))) return
    try {
      const res = await api.retryDead()
      pending.value = res.remaining
      deadCount.value = res.dead
      if (res.sent > 0) {
        refreshTemplates()
        toast(`已补传 ${res.sent} 条死信`, 'success')
      }
      if (res.restored > 0 && res.sent === 0) {
        toast(`已重新排队 ${res.restored} 条，但本次仍未成功，请检查服务器`, 'warning')
      }
    } catch {
      toast('重试失败，请稍后再试', 'error')
    }
  }

  /**
   * 今日生成数：托盘气泡要显示，但记录窗不常开。
   * 只靠记录窗回传的话，主窗运行时气泡永远是「今日 0」，所以主窗自己也拉一次。
   */
  async function refreshToday() {
    try {
      const res = await api.history('7')
      if (!res.ok) return
      const key = localDateKey(new Date())
      todayCount.value = (res.items ?? []).filter((it) => it.work_date === key).length
    } catch {
      /* 拉不到就保留上一次的数字，不值得打扰员工 */
    }
  }

  // ==================== 状态轮询 ====================
  let statusTimer: number | undefined

  /** 本地服务就绪后才开：服务器恢复后自动补传离线队列，员工不用记着手动点 */
  function startPolling() {
    statusTimer = window.setInterval(async () => {
      const wasOffline = !serverOk.value
      await refreshStatus(true)
      if (wasOffline && serverOk.value && pending.value > 0) {
        await flushQueue()
        if (pending.value === 0) toast('服务器已恢复，离线记录已自动补传', 'success')
      }
      // 别的机器/别的窗口可能又生成了，气泡数字跟着走
      refreshToday()
    }, 60_000)
  }

  onUnmounted(() => {
    if (statusTimer) window.clearInterval(statusTimer)
  })

  return {
    cfg,
    templates,
    partners,
    serverOk,
    ps,
    comAvailable,
    pending,
    deadCount,
    loadError,
    serverBusy,
    todayCount,
    refreshStatus,
    waitForSidecar,
    refreshTemplates,
    refreshDicts,
    flushQueue,
    retryDead,
    refreshToday,
    startPolling,
  }
}
