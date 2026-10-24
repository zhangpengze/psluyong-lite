/**
 * 主题状态与一键切换（composable）
 *
 * applyTheme 同步到 document（data-theme），toggleTheme 负责持久化并广播给
 * 记录窗 / 设置窗。主题以服务端配置为准（换机器/重装不丢偏好）。
 */

import { ref } from 'vue'
import { api } from '../api/sidecar'
import { broadcastEvent } from '../api/windowCtl'

export function useTheme() {
  const theme = ref<'dark' | 'light'>('dark')

  function applyTheme(t: 'dark' | 'light') {
    theme.value = t
    document.documentElement.dataset.theme = t
  }

  /**
   * 主窗头部一键切主题。
   *
   * 设置里原来也有「主题」——同一个开关两处都能改，员工每次都得想一下该去哪儿，
   * 于是撤了。按钮上显示的是**当前**主题的图标（深色=月亮、浅色=太阳），
   * title 说明点下去会变成什么，不用猜。
   */
  async function toggleTheme() {
    const next = theme.value === 'light' ? 'dark' : 'light'
    applyTheme(next)
    try {
      await api.saveConfig({ theme: next })
      // 记录窗/设置窗可能开着：它们各自 refreshStatus 时会读到新配置，这里只负责通知
      broadcastEvent('config-changed', { theme: next }).catch(() => {})
    } catch {
      /* 存不上面至少本次生效，下次启动回到旧主题，不值得弹错 */
    }
  }

  return { theme, applyTheme, toggleTheme }
}
