import { ApiError } from './envelope'

/**
 * 把一次失败变成一个给人看的句子。
 *
 * `-1` 是协议 / 连通性问题（信封层没有业务码可对照），此时只给原文；其余带上业务码，
 * 便于和后端日志、错误码表对齐——「业务码 20025」这句话本身就是排障线索。
 *
 * 抽到 `lib` 而不是每个页面各写一份：这是**给用户的**口径，两处一漂移，同一个 20025
 * 在两个页面上就会有两种说法，而用户会以为是两件事。
 */
export function errorText(value: unknown): string {
  if (value instanceof ApiError) {
    return value.code === -1 ? value.message : `${value.message}（业务码 ${value.code}）`
  }
  if (value instanceof Error) return value.message
  return String(value)
}

/**
 * 这次失败的业务码（不是协议/连通性问题时）。
 *
 * `errorText` 是给**用户**看的句子；分类处置（例如「20025 正在索引 → 跳过而不算失败」）
 * 需要的是一个数，不是一句话。两个工具一起用：先分类，再决定要不要把它当成失败。
 */
export function errorCodeOf(value: unknown): number {
  return value instanceof ApiError ? value.code : -1
}
