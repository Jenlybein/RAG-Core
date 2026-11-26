/**
 * 数据面 Key 串的形状：`rc_live_<行身份证>.<32 位十六进制>`，与后端
 * `app/utils/id_generator.py` 的 `new_api_key_value` / `split_api_key_value` 同构。
 *
 * 前端只用它做一件事：从用户**自己粘进来的**串里解出行身份证，于是「这把 Key 的配置」
 * 还能在已拉取的列表里找到——配置面板、白名单预筛、改配置深链全都保住，而密钥本体
 * 只在 `Authorization` 头里出现一次。
 *
 * 为什么不在前端也做一遍「够不够安全」的判断：行不存在、Key 被停用、租户被停用、
 * 密钥本体不对，这些只有后端说了算。这里只回答「这一串有没有可能是我们的 Key」，
 * 答错的后果是用户点一下按钮才发现，而不是看到一个错误的「你的 Key 无效」。
 */

export const API_KEY_PREFIX = 'rc_live_'
export const API_KEY_HEX_LENGTH = 32

const SEPARATOR = '.'
const HEX = /^[0-9a-fA-F]+$/

/** 解出串里的行身份证；形状不对返回 `null`（调用方负责提示「格式不对」）。 */
export function keyIdOf(plaintext: string): string | null {
  const trimmed = plaintext.trim()
  if (!trimmed.startsWith(API_KEY_PREFIX)) return null
  const rest = trimmed.slice(API_KEY_PREFIX.length)
  const separator = rest.indexOf(SEPARATOR)
  if (separator <= 0) return null
  const keyId = rest.slice(0, separator)
  const secret = rest.slice(separator + 1)
  if (secret.length !== API_KEY_HEX_LENGTH || !HEX.test(secret)) return null
  return keyId
}

/** 形状对不对。与 `keyIdOf(...) !== null` 同义，起名让调用点读得出来。 */
export function isWellFormedApiKey(plaintext: string): boolean {
  return keyIdOf(plaintext) !== null
}
