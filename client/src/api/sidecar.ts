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
  partners: string[]
  template_dir: string
  author_placeholder: string
  title_placeholder: string
  always_on_top: boolean
  dock_side: string
  win_x: number
  win_y: number
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
      machine: string
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

  history: () => request<{ ok: boolean; items: HistoryItem[]; error?: string }>('/history'),

  imageUrl: (recordId: number, full = false) =>
    `${BASE}/history/${recordId}/image${full ? '?full=1' : ''}`,

  previewUrl: (path: string) => `${BASE}/preview?path=${encodeURIComponent(path)}`,

  openFolder: (path: string) =>
    request<{ ok: boolean }>(`/open-folder?path=${encodeURIComponent(path)}`, { method: 'POST' }),

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
