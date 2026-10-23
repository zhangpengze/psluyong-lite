/**
 * sidecar 通信层
 *
 * sidecar 只绑 127.0.0.1:8765，前端所有动作都走这里，
 * 好处是错误提示能统一成员工看得懂的中文。
 */

const BASE = 'http://127.0.0.1:8765'

export interface ClientConfig {
  server_url: string
  api_key: string
  employee_name: string
  ps_path: string
  output_dir: string
  jpg_quality: number
  auto_clear: boolean
  last_template_id: string
  last_partner: string
  last_author: string
  partners: string[]
  template_dir: string
  author_placeholder: string
  title_placeholder: string
  /** 成品图命名：author=作者.jpg / author_title=作者_标题.jpg */
  filename_rule: string
  always_on_top: boolean
  /** 贴边方向：left / right / top / 空（不贴边） */
  dock_side: string
  /** 拖到屏幕边缘 24px 内松手自动贴边 */
  auto_dock: boolean
  /** 关闭主窗 = 隐藏到托盘（关掉则关闭即退出） */
  close_to_tray: boolean
  /** 主窗上次位置（-1 表示未记录） */
  win_x: number
  win_y: number
  /** 记录窗 / 设置窗各自的位置记忆 */
  history_win_x: number
  history_win_y: number
  settings_win_x: number
  settings_win_y: number
  theme: string
}

export interface TemplateItem {
  id: string
  name: string
  filename: string
  size: number
}

export interface ExportOk {
  ok: true
  image_path: string
  image_name: string
  warnings: string[]
  duration_ms: number
  uploaded: boolean
  queued: boolean
  upload_message: string
  record_id: number | null
}

export interface ExportFail {
  ok: false
  error: string
  warnings?: string[]
}

export type ExportResult = ExportOk | ExportFail

export interface ExportStatusRunning {
  ok: true
  state: 'running'
  finished: false
  elapsed_s: number
  heartbeat_stale: boolean
}

export interface HistoryItem {
  id: number
  work_date: string
  template_name: string
  partner_name: string
  author: string
  title: string
  status: string
  has_image: boolean
  created_at: string
  error_message?: string
  processing_ms?: number
  client_machine?: string
  ps_version?: string
  image_filename?: string
}

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
      config: ClientConfig
      pending: number
      dead: number
      busy: boolean
      machine: string
      version: string
      log_dir: string
    }>('/status'),

  getConfig: () => request<{ ok: boolean; config: ClientConfig }>('/config'),

  saveConfig: (patch: Partial<ClientConfig>) =>
    request<{ ok: boolean; config: ClientConfig }>('/config', {
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

  history: (range: '7' | '30' | 'all' = '7') =>
    request<{
      ok: boolean
      items: HistoryItem[]
      total: number
      range: string
      size: number
      error?: string
    }>(`/history?range=${range}`),

  /** version 传 created_at 之类的稳定值：图片是可变资源，没版本号浏览器会一直重拉 */
  imageUrl: (recordId: number, full = false, version: string | number = '') =>
    `${BASE}/history/${recordId}/image${full ? '?full=1' : ''}${
      version ? `${full ? '&' : '?'}v=${encodeURIComponent(String(version))}` : ''
    }`,

  openFolder: (path: string) =>
    request<{ ok: boolean }>(`/open-folder?path=${encodeURIComponent(path)}`, { method: 'POST' }),

  openLogs: () =>
    request<{ ok: boolean; path?: string; error?: string }>('/logs/open', { method: 'POST' }),

  flushQueue: () =>
    request<{ ok: boolean; sent: number; skipped: number; remaining: number; dead: number }>(
      '/queue/flush',
      {
        method: 'POST',
      },
    ),

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
