import { describe, expect, it } from 'vitest'

import { isBinaryFile, isSupportedUploadFile } from '../file'

/**
 * 文件过滤是「选文件夹」这条路径上唯一的准入判定，所以测试按用户看到的现象组织：
 * 选一个含 .DS_Store、.git/config、图片和五份 md 的目录，只应剩五份 md。
 */
describe('isSupportedUploadFile', () => {
  it('只放行 .md/.markdown/.txt/.docx/.pdf，并过滤隐藏文件', () => {
    // 放行：扩展名大小写不敏感。
    expect(isSupportedUploadFile({ name: 'guide.md' })).toBe(true)
    expect(isSupportedUploadFile({ name: 'GUIDE.MD' })).toBe(true)
    expect(isSupportedUploadFile({ name: 'notes.markdown' })).toBe(true)
    expect(isSupportedUploadFile({ name: 'todo.txt' })).toBe(true)
    expect(isSupportedUploadFile({ name: 'spec.docx' })).toBe(true)
    expect(isSupportedUploadFile({ name: 'manual.pdf' })).toBe(true)

    // 隐藏文件：文件名以点开头，以及放在隐藏目录里的普通文件名。
    expect(isSupportedUploadFile({ name: '.DS_Store' })).toBe(false)
    expect(isSupportedUploadFile({ name: '.env' })).toBe(false)
    expect(
      isSupportedUploadFile({ name: 'config', webkitRelativePath: 'corpus/.git/config' }),
    ).toBe(false)

    // 不支持的扩展名：包括「只认最后一段扩展名」这一点。
    expect(isSupportedUploadFile({ name: 'cover.png' })).toBe(false)
    expect(isSupportedUploadFile({ name: 'archive.tar.gz' })).toBe(false)
    expect(isSupportedUploadFile({ name: 'README' })).toBe(false)
  })
})

describe('isBinaryFile', () => {
  it('pdf 与 docx 是二进制，markdown/text 不是', () => {
    expect(isBinaryFile('manual.pdf')).toBe(true)
    expect(isBinaryFile('spec.DOCX')).toBe(true)
    expect(isBinaryFile('guide.md')).toBe(false)
    expect(isBinaryFile('todo.txt')).toBe(false)
  })
})
