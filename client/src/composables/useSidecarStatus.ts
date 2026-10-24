/**
 * sidecar 状态与数据（composable）
 *
 * 配置 / 模板 / 名单 / 队列 / 今日计数的加载与刷新，60 秒轮询（含断线恢复
 * 自动补传）、死信重试。所有数字以 /status 为权威，界面各处只读这里的状态。
 */

import { computed, onUnmounted, ref, type Ref } from 'vue'
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
  /** 积压里因配置受阻（401/403/404）的条数：修好 Key/地址后才会自动补上 */
  const pendingBlocked = ref(0)
  /** 受阻原因原文（服务端返回），悬停提示用 */
  const blockedReason = ref('')
  /**
   * 跑完了却既没进服务器也没进队列的次数（本地磁盘/服务异常）。
   * 正常恒为 0——sidecar 有后台结算 + 启动对账兜底，非 0 说明那两条路也失败了，
   * 员工必须看见，否则等到月底对账才发现少了几条记录。
   */
  const unsynced = ref(0)
  const unsyncedHint =
    '这几次生成的图片还在本机输出目录里，但记录没能自动上报。' +
    '软件会继续重试；一直这样请把「设置 → 关于」里的日志发给管理员。'
  /**
   * 配置受阻的说明文案：员工改对 API Key / 服务器地址后，这些记录会自动补传。
   * 放在这里而不是 App.vue——文案依赖的就是上面这四个 ref，散在视图里迟早不同步。
   */
  const blockedHint = computed(
    () =>
      `${pendingBlocked.value} 条记录因配置问题传不上去（${blockedReason.value || '鉴权或地址错误'}）。` +
      '记录都还在本地，去「设置 → 账号」检查 API Key 和服务器地址，改对后会自动补传。',
  )
  const loadError = ref('')
  /** sidecar 权威的生成中标志（切窗口后仍然准确；录入页实时上报的那个在 App） */
  const serverBusy = ref(false)
  // 记录窗今日生成数（HistoryPanel 回传 / 自己拉，用于托盘气泡）
  const todayCount = ref(0)
  /** 配置自愈提示只弹一次（每个窗口的进程各一次） */
  let recoveryNotified = false
  /** 待补报提示同理：只弹一次，别每 60 秒骚扰一遍 */
  let unsyncedNotified = false

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
      pendingBlocked.value = s.pending_blocked ?? 0
      blockedReason.value = s.blocked_reason ?? ''
      unsynced.value = s.unsynced ?? 0
      serverBusy.value = !!s.busy
      loadError.value = ''
      if (unsynced.value > 0 && !unsyncedNotified) {
        unsyncedNotified = true
        toast(`有 ${unsynced.value} 次生成还没同步到服务器，正在自动重试`, 'warning')
      }
      // 配置被自愈过（文件损坏）只提示一次：员工需要知道"Key 可能要重填"，
      // 但每 60 秒弹一遍就是骚扰。进程内只弹一次，重启后由 sidecar 重新给。
      const recovered = (s.config_recovered ?? '').trim()
      if (recovered && !recoveryNotified) {
        recoveryNotified = true
        toast(`${recovered}。请在「设置 → 账号」核对服务器地址和 API Key`, 'warning')
      }
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
      // 受阻条数必须跟着刷新：旧版补传把 401/403 的记录直接删了，界面上
      // "待补传 3 条"变 0 看着像成功；现在记录会留着，必须让员工看见原因
      pendingBlocked.value = res.blocked ?? 0
      blockedReason.value = res.block_reason ?? ''
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
    pendingBlocked,
    blockedReason,
    blockedHint,
    unsynced,
    unsyncedHint,
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
