/**
 * 哪个窗口负责"会往服务器写东西"的自动动作
 *
 * 三个窗口（主窗 #/main、记录窗 #/history、设置窗 #/settings）是三个独立的
 * WebView，各跑一份 App.vue，启动时都会走一遍 startAfterReady → flushQueue()。
 * 于是同一批积压被同时 POST /queue/flush 两三次：
 *   - 成功记录：服务端唯一索引挡住（409），白跑一趟上传
 *   - 5xx 的记录：两边各 failures +1，MAX_ATTEMPTS 提前用完，本该还能重试的
 *     记录被提前丢进死信（真丢数据）
 *   - 两边同时改同一个 json：后写的覆盖前写的
 * 失败记录的尝试次数、耗时统计也会被重复上报，主管看到的失败数虚高。
 *
 * 所以口径定在这里：**只有主窗补传/拉今日数**，辅助窗只读 /status（界面上的
 * 待补传、死信数字照样跟着刷新）。sidecar 那边另有一把非阻塞锁兜底
 * （queue_flush），窗口这边错了也不会真的提交两遍。
 */

export type WindowRoute = 'main' | 'history' | 'settings'

/**
 * 这个窗口是不是"队列的主人"。
 *
 * 未知路由一律返回 false：宁可少补传一次（下个 60 秒还会补），也不要多写一遍。
 * 主窗的 hash 是空的，routeFromHash 会兜底成 'main'，所以主窗不会被误判。
 */
export function ownsQueue(route: string): boolean {
  return route === 'main'
}
