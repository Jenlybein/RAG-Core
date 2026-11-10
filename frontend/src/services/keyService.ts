import { consoleHttp } from '../lib/consoleHttp'
import type {
  KeyData,
  KeyDeleteReceipt,
  KeyIssueRequest,
  KeyListData,
  KeyPolicyPatch,
  KeyPolicyPatchResult,
} from '../types/key'

/**
 * 控制面 Key 与密钥配置接口（票 #91 后端，票 #92 前端）。
 *
 * 只负责「怎么请求」：路径、请求体形状、把信封里的 `data` 摊平。三条写操作都由
 * `consoleHttp` 发，因而自动带上 `x-requested-with`——缺了它后端回 20011，而 20011
 * 不是掉登录。这里**不走**数据面客户端：拿 Key 管平台会让两套身份混在一起。
 */

/** Key 列表的 query key：签发 / 删除 / 改配置之后都失效它，列表立刻回显。 */
export const KEYS_QUERY_KEY = ['keys'] as const

/** 本租户的 Key 列表（每行带明文、masked 与它引用的配置）。 */
export function fetchKeys(): Promise<KeyData[]> {
  return consoleHttp.get<KeyListData>('/console/keys').then((data) => data.keys)
}

/**
 * 签发一把 Key，连带建一套配置。
 *
 * `label` 只是备注名（鉴权永远不读它）；全空白按「没填」处理——发一个空的 `label`
 * 和发 `null` 对后端是同一件事，但请求体里少一个键更干净。
 */
export function createKey(payload: KeyIssueRequest): Promise<KeyData> {
  const label = (payload.label ?? '').trim()
  const body: KeyIssueRequest = label === '' ? { policy: payload.policy } : { label, policy: payload.policy }
  return consoleHttp.post<KeyData>('/console/keys', body)
}

/** 删一把 Key：配置独立于 Key，删凭证不连带删配置。 */
export function deleteKey(keyId: string): Promise<KeyDeleteReceipt> {
  return consoleHttp.delete<KeyDeleteReceipt>(`/console/keys/${keyId}`)
}

/**
 * 改一套配置（部分更新，改完下一次调用即生效）。
 *
 * 补丁由 `utils/keyPolicy.ts` 的 `buildPolicyPatch` 生成：只含真的变了的字段。
 * 这里不再过滤——在这一层再写一遍「哪些字段算变了」就出现两份判定。
 */
export function patchPolicy(
  policyId: string,
  patch: KeyPolicyPatch,
): Promise<KeyPolicyPatchResult> {
  return consoleHttp.patch<KeyPolicyPatchResult>(`/console/key-policies/${policyId}`, patch)
}
