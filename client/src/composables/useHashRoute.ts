/**
 * hash 路由（composable）
 *
 * 三个窗口，各看各的页面：主窗 #/main、记录窗 #/history、设置窗 #/settings。
 * hash 路由而不是多入口 HTML：Rust 只建窗口，页面由这里分发，
 * 共享的状态（配置/模板/主题/toast）也集中在 App 这一层。
 */

import { onMounted, onUnmounted, ref } from 'vue'

export type Route = 'main' | 'history' | 'settings'

const ROUTES: Route[] = ['main', 'history', 'settings']

function routeFromHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, '')
  return (ROUTES as string[]).includes(raw) ? (raw as Route) : 'main'
}

export function useHashRoute() {
  const route = ref<Route>(routeFromHash())

  const onHashChange = () => {
    route.value = routeFromHash()
  }

  onMounted(() => window.addEventListener('hashchange', onHashChange))
  onUnmounted(() => window.removeEventListener('hashchange', onHashChange))

  return route
}
