/**
 * 时间展示。
 *
 * 后端的时间列由 `app/db/base.py` 的 `now()`（`datetime.now()`）产生，序列化出来是
 * **没有时区后缀的本地时间**（如 `2026-11-10T09:05:07`）。这里刻意不在前端补 `Z`：
 * 补了会把服务端的本地时间当成 UTC 再换算一次，班次交接时看审计会整体偏移几小时。
 * 按字符串解析到本地时间，与写入方同一套钟。
 */
export function formatDateTime(value: string | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'

  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    // 解析不了就原样给出：审计页的时间是证据，不能变成「Invalid Date」或一个假的时间。
    return value
  }

  const pad = (n: number): string => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  )
}
