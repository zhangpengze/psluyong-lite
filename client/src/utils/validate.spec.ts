import { describe, expect, it } from 'vitest'
import { isHttpUrl, isWinAbsPath } from './validate'

describe('isHttpUrl', () => {
  it('http / https 开头即通过', () => {
    expect(isHttpUrl('http://192.168.1.10:8080')).toBe(true)
    expect(isHttpUrl('  https://erp.example.cn')).toBe(true)
  })

  it('缺协议或空串不通过（员工最常填错的形态）', () => {
    expect(isHttpUrl('192.168.1.10:8080')).toBe(false)
    expect(isHttpUrl('')).toBe(false)
  })
})

describe('isWinAbsPath', () => {
  it('盘符开头的完整路径通过', () => {
    expect(isWinAbsPath('D:\\期刊录用')).toBe(true)
    expect(isWinAbsPath('E:/psluyong-lite/templates')).toBe(true)
  })

  it('留空通过：留空表示用默认目录 / 自动识别', () => {
    expect(isWinAbsPath('')).toBe(true)
    expect(isWinAbsPath('   ')).toBe(true)
  })

  it('相对路径 / 纯文字不通过', () => {
    expect(isWinAbsPath('期刊录用')).toBe(false)
    expect(isWinAbsPath('.\\output')).toBe(false)
  })
})
