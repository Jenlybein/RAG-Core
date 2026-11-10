import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../lib/envelope'
import type { DocumentUploadReceipt } from '../../types/document'
import { useUploadDocuments, type UploadFileItem } from '../useUploadDocuments'

function makeItem(fileName: string): UploadFileItem {
  // 编排只读 fileName 与把 file 原样交出，不需要真实 File；用最小替身避免测试依赖 DOM。
  return { fileName, file: { name: fileName } as unknown as File }
}

function makeReceipt(documentId: string, status = 3): DocumentUploadReceipt {
  return {
    document_id: documentId,
    kb_id: 'kb-1',
    status,
    chunk_count: 0,
    error_message: null,
    source_type: 'markdown',
  }
}

describe('useUploadDocuments', () => {
  it('串行上传：上一份回执回来之前不发起下一份', async () => {
    const calls: string[] = []
    let inFlight = 0
    let maxInFlight = 0

    const upload = async (item: UploadFileItem): Promise<DocumentUploadReceipt> => {
      inFlight += 1
      maxInFlight = Math.max(maxInFlight, inFlight)
      calls.push(item.fileName)
      // 留一个真实的异步间隙：若实现改用 Promise.all，这里会观察到并发。
      await new Promise((resolve) => setTimeout(resolve, 5))
      inFlight -= 1
      return makeReceipt(`doc-${calls.length}`)
    }

    const uploader = useUploadDocuments({ upload })
    await uploader.uploadAll('kb-1', [makeItem('a.md'), makeItem('b.md'), makeItem('c.md')])

    expect(calls).toEqual(['a.md', 'b.md', 'c.md'])
    expect(maxInFlight).toBe(1)
    expect(uploader.results.value.map((row) => row.fileName)).toEqual(['a.md', 'b.md', 'c.md'])
  })

  it('单个文件失败不中断后续文件，并汇总成功/失败数', async () => {
    const upload = vi.fn(async (item: UploadFileItem): Promise<DocumentUploadReceipt> => {
      if (item.fileName === 'bad.pdf') {
        throw new ApiError(20028, 'data', '该知识库不接受 pdf 格式，允许的格式：markdown')
      }
      return makeReceipt(`doc-${item.fileName}`)
    })

    const uploader = useUploadDocuments({ upload })
    // 第三份仍要被尝试：失败只影响自己那一行，不是整批的结论。
    await expect(
      uploader.uploadAll('kb-1', [makeItem('a.md'), makeItem('bad.pdf'), makeItem('c.md')]),
    ).resolves.toBeUndefined()

    const rows = uploader.results.value
    expect(rows.map((row) => row.status)).toEqual(['processing', 'failed', 'processing'])
    // 展示的是 `lib/envelope.ts` 里那份人话（20028 的既定文案），业务码单独留给页面补出来。
    expect(rows[1]?.errorMessage).toBe('该格式不被这个知识库接收：换文件或换库')
    expect(rows[1]?.errorCode).toBe(20028)
    expect(upload).toHaveBeenCalledTimes(3)
    expect(uploader.succeededCount.value).toBe(2)
    expect(uploader.failedCount.value).toBe(1)
  })
})
