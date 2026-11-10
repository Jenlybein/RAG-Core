/**
 * 控制面写操作的防跨站头。与后端 `app/core/console_auth.py` 的常量一一对应：
 * 缺了它就是 20011，而 20011 不是掉登录。
 */
export const WRITE_HEADER_NAME = 'x-requested-with'
export const WRITE_HEADER_VALUE = 'XMLHttpRequest'

const WRITE_METHODS = new Set(['post', 'put', 'patch', 'delete'])

/**
 * 只给会改东西的方法加头。GET 也加的话，每个读请求都会变成「非简单请求」，
 * 一旦哪天真的跨源就会平白多一次预检；更重要的是，这会让「写操作才有这道门槛」失真。
 */
export function needsWriteHeader(method?: string): boolean {
  return WRITE_METHODS.has((method ?? 'get').toLowerCase())
}
