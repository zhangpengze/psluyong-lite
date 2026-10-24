/**
 * 设置表单的字段校验（父面板保存前拦截 + 各分组内联提示共用一份规则，
 * 避免两处各写一版、改了一处忘另一处）
 */

/** 服务器地址必须以 http:// 或 https:// 开头 */
export function isHttpUrl(v: string): boolean {
  return v.trim().startsWith('http')
}

/**
 * 路径字段必须是完整路径（Windows 盘符开头）。
 * 不拦的代价：员工填个「姓名」当时看不出问题，生成时 PS 才报
 * 「文件不存在」，谁都想不到是这里填错了（真实踩坑）。
 * 留空视为「用默认/自动识别」，允许通过；具体字段各自的默认语义由调用方提示。
 */
const WIN_ABS = /^[A-Za-z]:[\\/]/

export function isWinAbsPath(v: string): boolean {
  return !v.trim() || WIN_ABS.test(v.trim())
}
