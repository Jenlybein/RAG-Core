/**
 * 调试台那把 Key 的**会话级**落盘位置。
 *
 * 为什么单独一个模块，而不是把 `sessionStorage` 直接写进页面：键名是跨文件契约——
 * `/keys` 的「去调试台试用」要往同一个键里写，调试台要在挂载时读它。两处各写一遍字符串，
 * 拼错一个冒号就是「签发了新 Key、刷新后调试台又空了」，而且这种错不会报。
 *
 * 为什么是 `sessionStorage` 而不是 `localStorage`：Key 明文的生命周期应当跟着这一次
 * 浏览器会话结束，而不是因为「上次来过」就在另一天复活。刷新页面还在是需要的（这就是
 * 这条需求本身），换一个会话不在。
 *
 * 为什么读写都吞异常：隐私模式、被策略禁用的 storage 会在**读取属性时**就抛。这里顺手
 * 降级成「只进内存」，页面照常能用；把异常抛给用户，等于让他在一个开发者无从修复的
 * 环境问题上卡住。调用方拿到 `null` 只意味着「没有预填值」，不是错误。
 */

/** 键名固定：`/keys` 与 `/retrieve` 两端都从这里取。 */
export const RETRIEVE_API_KEY_STORAGE_KEY = 'rag-console:api-key'

/** 只声明用到的三个方法：用例可以给一个假对象，不必造出完整的 `Storage`。 */
export interface KeyStorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

function resolveStorage(storage: KeyStorageLike | undefined): KeyStorageLike {
  return storage ?? sessionStorage
}

/** 读出上次会话留下的 Key；空白串按「没有」处理（空 Bearer 会换一个像失效的 20010）。 */
export function readRetrieveApiKey(storage?: KeyStorageLike): string | null {
  try {
    const value = resolveStorage(storage).getItem(RETRIEVE_API_KEY_STORAGE_KEY)
    const trimmed = value === null ? '' : value.trim()
    return trimmed === '' ? null : trimmed
  } catch {
    return null
  }
}

/** 记住这次应用的 Key；空白串等价于清空，不留下一个「看起来有值」的空字符串。 */
export function writeRetrieveApiKey(plaintext: string, storage?: KeyStorageLike): void {
  try {
    const trimmed = plaintext.trim()
    const target = resolveStorage(storage)
    if (trimmed === '') {
      target.removeItem(RETRIEVE_API_KEY_STORAGE_KEY)
      return
    }
    target.setItem(RETRIEVE_API_KEY_STORAGE_KEY, trimmed)
  } catch {
    // 存储不可用：这一次会话里 Key 仍在内存（`lib/activeKey`），只是刷新后会丢。
  }
}

/** 清掉会话里的 Key（登出 / 主动作废这种「不该再自动恢复」的场合）。 */
export function clearRetrieveApiKey(storage?: KeyStorageLike): void {
  try {
    resolveStorage(storage).removeItem(RETRIEVE_API_KEY_STORAGE_KEY)
  } catch {
    // 同上：清不掉也不影响内存里那份已经被 `setActiveKey(null)` 抹掉的值。
  }
}
