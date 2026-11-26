import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../lib/envelope'
import type { DocumentUploadReceipt } from '../../types/document'
import { DOCUMENT_STATUS } from '../../utils/documentStatus'
import { useUploadDocuments, type UploadFileItem } from '../useUploadDocuments'

function makeItem(fileName: string): UploadFileItem {
  // 编排只读 fileName 与把 file 原样交出，不需要真实 File；用最小替身避免测试依赖 DOM。
  return { fileName, file: { name: fileName } as unknown as File }
}

function makeReceipt(
  documentId: string,
  status: number = DOCUMENT_STATUS.QUEUED,
): DocumentUploadReceipt {
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
    // 上传回执一律是 4（排队中）：后端只登记 + 投递，还没人接。
    expect(rows.map((row) => row.status)).toEqual(['queued', 'failed', 'queued'])
    // 展示的是 `lib/envelope.ts` 里那份人话（20028 的既定文案），业务码单独留给页面补出来。
    expect(rows[1]?.errorMessage).toBe('该格式不被这个知识库接收：换文件或换库')
    expect(rows[1]?.errorCode).toBe(20028)
    expect(upload).toHaveBeenCalledTimes(3)
    expect(uploader.succeededCount.value).toBe(2)
    expect(uploader.failedCount.value).toBe(1)
  })

  it('排队中与索引中都不显示分块数（后台还没开始切）', async () => {
    const upload = vi.fn(async (item: UploadFileItem) =>
      makeReceipt(
        `doc-${item.fileName}`,
        item.fileName === 'done.md'
          ? DOCUMENT_STATUS.SUCCESS
          : DOCUMENT_STATUS.QUEUED,
      ),
    )

    const uploader = useUploadDocuments({ upload })
    await uploader.uploadAll('kb-1', [makeItem('queued.md'), makeItem('done.md')])

    const [queued, done] = uploader.results.value
    expect(queued?.chunkCount).toBeNull()
    // 成功的那一行如实显示 0 块：它是结论，不是「还没算」。
    expect(done?.chunkCount).toBe(0)
  })

  it('四态各自折成自己的行状态，未知值不猜', async () => {
    const statuses = [1, 2, 3, 4, 99]
    const upload = vi.fn(async (item: UploadFileItem) => {
      const index = Number.parseInt(item.fileName, 10)
      return makeReceipt(`doc-${index}`, statuses[index])
    })

    const uploader = useUploadDocuments({ upload })
    await uploader.uploadAll('kb-1', statuses.map((_, index) => makeItem(`${index}`)))

    expect(uploader.results.value.map((row) => row.status)).toEqual([
      'success',
      'failed',
      'processing',
      'queued',
      'unknown',
    ])
    // 「成功」这一栏把排队中与索引中都算上：后端受理了就是这次上传的成功。
    expect(uploader.succeededCount.value).toBe(3)
    expect(uploader.failedCount.value).toBe(1)
  })
})
