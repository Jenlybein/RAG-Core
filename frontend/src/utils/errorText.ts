import { ApiError } from '../lib/envelope'

/**
 * 页面展示用的错误文案。
 *
 * 业务码到人话的映射只发生在 `lib/envelope.ts`（唯一一处），这里不再翻译一次，只负责从
 * `ApiError` / 原生异常里取出已经算好的 `message`。三个管理员页面的 `ElMessage` 都走它，
 * 免得每页各写一份 `instanceof` 判断、各漏一种情况。
 */
export function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return String(error)
}
