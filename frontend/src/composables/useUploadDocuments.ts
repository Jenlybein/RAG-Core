import { computed, ref } from 'vue'

import { ApiError } from '../lib/envelope'
import type { DocumentUploadReceipt } from '../types/document'

/** 一批待上传文件里的一份。`fileName` 是展示名（选文件夹时带相对路径）。 */
export interface UploadFileItem {
  file: File
  fileName: string
}

/** 结果行的状态：pending 只在本份请求还没回来时出现。 */
export type UploadRowStatus = 'pending' | 'success' | 'failed' | 'processing' | 'unknown'

/** 上传结果行。`chunk_count` 只在终态有意义；处理中保持 null 由页面显示「—」。 */
export interface UploadResultRow {
  key: string
  fileName: string
  status: UploadRowStatus
  documentId: string | null
  chunkCount: number | null
  errorMessage: string | null
  errorCode: number | null
}

export interface UseUploadDocumentsOptions {
  /**
   * 注入「上传一份」的实现。编排只负责顺序与失败隔离，网络细节归 services：
   * 测试因此能直接观察串行性，而不必 mock axios 或连后端。
   */
  upload: (item: UploadFileItem, kbId: string) => Promise<DocumentUploadReceipt>
}

/** 回执里的整数状态折成页面可直接分文案的枚举；未知值不猜，交给页面显「未知状态」。 */
function rowStatusOf(receiptStatus: number): UploadRowStatus {
  if (receiptStatus === 1) return 'success'
  if (receiptStatus === 2) return 'failed'
  if (receiptStatus === 3) return 'processing'
  return 'unknown'
}

function errorMessageOf(error: unknown): string {
  // ApiError 的 message 已经是译成人话的那一份；后端原文留在 error.backendMsg 里排障用。
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return String(error)
}

export function useUploadDocuments(options: UseUploadDocumentsOptions) {
  const results = ref<UploadResultRow[]>([])
  const running = ref(false)
  let seq = 0

  // 「成功」包含 status=3：上传只做登记，后端已受理并投递索引任务就是这一次成功；
  // 把它算成失败或未完成会让整批上传看起来全灭。
  const succeededCount = computed(
    () => results.value.filter((row) => row.status === 'success' || row.status === 'processing').length,
  )
  const failedCount = computed(
    () => results.value.filter((row) => row.status === 'failed').length,
  )

  function nextRow(fileName: string): UploadResultRow {
    seq += 1
    // 不同子目录可能同名，所以 key 由序号主导，文件名只用于可读性。
    return {
      key: `${seq}-${fileName}`,
      fileName,
      status: 'pending',
      documentId: null,
      chunkCount: null,
      errorMessage: null,
      errorCode: null,
    }
  }

  async function uploadAll(kbId: string, items: UploadFileItem[]): Promise<void> {
    if (running.value) return
    running.value = true
    // 结果只属于当前这一批：重新上传时旧回执留着会混成「这一批」的结论。
    results.value = []
    try {
      // 串行是**需求**而不是实现细节：并发上传会一起撞单库文档数与并发处理数配额，
      // 失败原因也会互相掩盖；`for...of + await` 是唯一保证同一时刻只有一份在路上的写法。
      for (const item of items) {
        const index = results.value.push(nextRow(item.fileName)) - 1
        try {
          const receipt = await options.upload(item, kbId)
          results.value[index] = {
            key: results.value[index]!.key,
            fileName: item.fileName,
            status: rowStatusOf(receipt.status),
            documentId: receipt.document_id,
            // status=3 的 chunk_count 恒为 0（后台还没开始切），null 让页面显示「—」，
            // 避免把「尚未切片」误读成「没切出东西」。
            chunkCount: receipt.status === 3 ? null : receipt.chunk_count,
            errorMessage: receipt.error_message,
            errorCode: null,
          }
        } catch (error) {
          // 单个失败只落自己那一行：后面的文件仍要尝试，整批不 throw。
          results.value[index] = {
            key: results.value[index]!.key,
            fileName: item.fileName,
            status: 'failed',
            documentId: null,
            chunkCount: null,
            errorMessage: errorMessageOf(error),
            errorCode: error instanceof ApiError ? error.code : null,
          }
        }
      }
    } finally {
      running.value = false
    }
  }

  return { results, running, succeededCount, failedCount, uploadAll }
}
