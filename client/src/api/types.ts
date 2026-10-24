/**
 * sidecar 通信层的类型定义
 *
 * 从 sidecar.ts 拆出来只有一个理由：那个文件同时装着"类型"和"二十几个接口方法"，
 * 加一个按钮就顶到 300 行上限。类型是稳定的、方法是常变的，分开之后
 * 两次改动互不干扰。
 *
 * sidecar.ts 会把这些名字原样再导出一次，调用方继续 `from '../api/sidecar'` 即可。
 */

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
  /** 员工自己点的取消：不是失败，不该计入失败率，也不必"重新填写" */
  cancelled?: boolean
  /** PS 崩溃导致结果未知，需要人去输出目录确认 */
  uncertain?: boolean
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

/** 时间区间：today/7/30/all 是快捷档，custom 由外部给起止日期 */
export type HistoryRange = 'today' | '7' | '30' | 'all' | 'custom'

/** 本机 Photoshop 的识别结果（sidecar 自动探测，任意版本/任意盘符） */
export interface PsInfo {
  path: string
  version: string
  /** config=手动指定 / app-paths=注册表 / uninstall=安装信息 / scan=目录扫描 / none=未识别 */
  source: string
  source_label: string
  found: boolean
  /** 手填的路径已失效：生成用自动识别的顶替，界面要提醒一句 */
  override_missing: boolean
}

/**
 * 测试连接的四档结论：地址格式错 / 连不上 / 地址通了但 Key 不对 / 都通。
 * 分开报的意义：员工能自己改对，而不是笼统的"连接失败"然后去找运维。
 */
export interface ConnectionTestResult {
  level: 'ok' | 'invalid' | 'unreachable' | 'key_bad' | 'server_error'
  message: string
  server_reachable: boolean
  key_ok: boolean
  status?: number
}
