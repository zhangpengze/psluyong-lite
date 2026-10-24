/**
 * sidecar 通信层
 *
 * sidecar 只绑 127.0.0.1:8765，前端所有动作都走这里，
 * 好处是错误提示能统一成员工看得懂的中文。
 *
 * 类型定义在 ./types（这个文件只放"怎么调"，不放"长什么样"），
 * 这里再导出一次，调用方继续 `from '../api/sidecar'` 就行。
 */

import type {
  ClientConfig,
  ConnectionTestResult,
  ExportResult,
  ExportStatusRunning,
  HistoryItem,
  HistoryRange,
  PsInfo,
  TemplateItem,
} from './types'

export type {
  ClientConfig,
  ConnectionTestResult,
  ExportFail,
  ExportOk,
  ExportResult,
  ExportStatusRunning,
  HistoryItem,
  HistoryRange,
  PsInfo,
  TemplateItem,
} from './types'

const BASE = 'http://127.0.0.1:8765'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new Error('本地服务未启动，请重启客户端')
  }

  const text = await resp.text()
  let data: any = {}
  try {
    data = text ? JSON.parse(text) : {}
  } catch {
    throw new Error(`本地服务返回异常（HTTP ${resp.status}）`)
  }
  return data as T
}

export const api = {
  status: () =>
    request<{
      ok: boolean
      server_reachable: boolean
      com_available: boolean
      platform: string
      ps: PsInfo
      config: ClientConfig
      pending: number
      dead: number
      /** 跑完但还没结算（既没进服务器也没进队列）的次数，正常恒为 0 */
      unsynced?: number
      /** 其中因环境/配置受阻（401/403/404）的条数——修好 Key 或地址后自动补传 */
      pending_blocked?: number
      /** 受阻原因（服务端原文），界面上直接给员工看 */
      blocked_reason?: string
      busy: boolean
      machine: string
      version: string
      log_dir: string
      /** 配置被自愈过（损坏→从备份恢复 / 重置默认值）的说明，空串=没事 */
      config_recovered?: string
    }>('/status'),

  /** 重新识别本机 Photoshop（装完/换版本后点一下） */
  detectPs: () => request<{ ok: boolean; ps: PsInfo }>('/ps/detect', { method: 'POST' }),

  getConfig: () => request<{ ok: boolean; config: ClientConfig }>('/config'),

  /**
   * 用表单里此刻填的地址与 Key 测一次连接（不读磁盘、不保存）。
   * level 区分了"地址不通"和"Key 不对"，界面必须如实转述。
   */
  testConfig: (serverUrl: string, apiKey: string) =>
    request<{ ok: boolean; result: ConnectionTestResult }>('/config/test', {
      method: 'POST',
      body: JSON.stringify({ server_url: serverUrl, api_key: apiKey }),
    }),

  saveConfig: (patch: Partial<ClientConfig>) =>
    request<{ ok: boolean; config: ClientConfig; error?: string }>('/config', {
      method: 'POST',
      body: JSON.stringify(patch),
    }),

  templates: () =>
    request<{
      ok: boolean
      items: TemplateItem[]
      template_dir: string
      warning?: string
      error?: string
    }>('/templates'),

  /** 上传 PSD：multipart 不能走 request()，那里写死了 JSON 头 */
  uploadTemplates: async (files: File[]) => {
    const form = new FormData()
    files.forEach((f) => form.append('files', f, f.name))

    let resp: Response
    try {
      resp = await fetch(`${BASE}/templates/upload`, { method: 'POST', body: form })
    } catch {
      throw new Error('本地服务未启动，请重启客户端')
    }
    const text = await resp.text()
    try {
      return JSON.parse(text) as {
        ok: boolean
        saved: string[]
        errors: string[]
        items: TemplateItem[]
      }
    } catch {
      throw new Error(`上传失败（HTTP ${resp.status}）`)
    }
  },

  deleteTemplate: (name: string) =>
    request<{ ok: boolean; items?: TemplateItem[]; error?: string }>('/templates/delete', {
      method: 'POST',
      body: JSON.stringify({ name }),
    }),

  openTemplateDir: () =>
    request<{ ok: boolean; path?: string; error?: string }>('/templates/open-dir', {
      method: 'POST',
    }),

  dicts: () => request<{ ok: boolean; grouped: Record<string, string[]> }>('/dicts'),

  exportImage: (payload: {
    template_id: string
    author: string
    title: string
    partner_name: string
  }) =>
    request<{ ok: true; task_id: string; state: 'started' } | { ok: false; error: string; busy?: boolean }>(
      '/export',
      { method: 'POST', body: JSON.stringify(payload) },
    ),

  exportStatus: (taskId: string) =>
    request<ExportStatusRunning | ExportResult & { finished: true; task_id: string }>(
      `/export/status?task_id=${encodeURIComponent(taskId)}`,
    ),

  /** 取消生成：PS 挂住时员工唯一的自救手段 */
  cancelExport: (taskId: string) =>
    request<{ ok: boolean; task_id?: string; error?: string; message?: string }>('/export/cancel', {
      method: 'POST',
      body: JSON.stringify({ task_id: taskId }),
    }),

  /**
   * 记录列表。range 是快捷档（today/7/30/all），custom 由 start/end 决定
   * （YYYY-MM-DD，两头可以只给一头，sidecar 会补齐并回显真正生效的 start/end）。
   */
  history: (range: HistoryRange = '7', start = '', end = '') =>
    request<{
      ok: boolean
      items: HistoryItem[]
      total: number
      range: string
      /** 回显：真正生效的起止日期，界面直接显示，避免"只填起始日"时歧义 */
      start: string
      end: string
      size: number
      error?: string
    }>(
      `/history?range=${range}` +
        (start ? `&start=${encodeURIComponent(start)}` : '') +
        (end ? `&end=${encodeURIComponent(end)}` : ''),
    ),

  /** version 传 created_at 之类的稳定值：图片是可变资源，没版本号浏览器会一直重拉 */
  imageUrl: (recordId: number, full = false, version: string | number = '') =>
    `${BASE}/history/${recordId}/image${full ? '?full=1' : ''}${
      version ? `${full ? '&' : '?'}v=${encodeURIComponent(String(version))}` : ''
    }`,

  openFolder: (path: string) =>
    request<{ ok: boolean }>(`/open-folder?path=${encodeURIComponent(path)}`, { method: 'POST' }),

  /**
   * 一键诊断包：sidecar 生成 zip 存到本机固定目录并打开。
   * 不用 <a download>，因为 Tauri 窗口在部分 Windows 版本上点了没反应。
   */
  saveDiagnostics: () =>
    request<{ ok: boolean; path?: string; dir?: string; error?: string }>('/diagnostics/save', {
      method: 'POST',
    }),

  openLogs: () =>
    request<{ ok: boolean; path?: string; error?: string }>('/logs/open', { method: 'POST' }),

  flushQueue: () =>
    request<{
      ok: boolean
      sent: number
      skipped: number
      remaining: number
      dead: number
      /** 当前受阻条数与原因（401/403/404 保留在队列里，不会像以前那样被静默删除） */
      blocked?: number
      block_reason?: string
    }>('/queue/flush', { method: 'POST' }),

  retryDead: () =>
    request<{
      ok: boolean
      restored: number
      failed: number
      sent: number
      skipped: number
      remaining: number
      dead: number
    }>('/queue/retry-dead', { method: 'POST' }),
}
