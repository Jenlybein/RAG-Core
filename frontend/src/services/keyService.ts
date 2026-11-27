import { consoleHttp } from '../lib/consoleHttp'
import type {
  IssuedKeyData,
  KeyData,
  KeyDeleteReceipt,
  KeyIssueRequest,
  KeyListData,
  KeyPolicyPatch,
  KeyPolicyPatchResult,
  KeyRevokePreviousReceipt,
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

/** 本租户的 Key 列表（每行带打码形态与它引用的配置；原文不在其中）。 */
export function fetchKeys(): Promise<KeyData[]> {
  return consoleHttp.get<KeyListData>('/console/keys').then((data) => data.keys)
}

/**
 * 签发一把 Key，连带建一套配置。
 *
 * 返回的 `key_plaintext` 是**唯一**一次能看到完整 Key 的地方：库里只存 scrypt 哈希，
 * 弹窗关掉之后再想用它只能重签。`label` 只是备注名（鉴权永远不读它）；全空白按「没填」
 * 处理——发一个空的 `label` 和发 `null` 对后端是同一件事，但请求体里少一个键更干净。
 */
export function createKey(payload: KeyIssueRequest): Promise<IssuedKeyData> {
  const label = (payload.label ?? '').trim()
  const body: KeyIssueRequest = label === '' ? { policy: payload.policy } : { label, policy: payload.policy }
  return consoleHttp.post<IssuedKeyData>('/console/keys', body)
}

/** 删一把 Key：配置独立于 Key，删凭证不连带删配置。 */
export function deleteKey(keyId: string): Promise<KeyDeleteReceipt> {
  return consoleHttp.delete<KeyDeleteReceipt>(`/console/keys/${keyId}`)
}

/**
 * 重新生成这把 Key 的**密钥本体**（轮换）。
 *
 * 与签发刻意分开：签发是加法（多一把 Key，旧的不受影响），这里是替换（同一把 Key 换凭证，
 * 旧明文进入 24 小时过渡期）。两者的回执都是 `IssuedKeyData`——原文同样只出现这一次；
 * 轮换的回执里还带着这次写进数据库的过渡截止时刻 `prev_key_expires_at`，界面据此把
 * 「旧 Key 还能用到几点」说清楚，不在前端自己按当前时刻再推一遍 24 小时。
 *
 * 两条后端已经保证、界面不重复实现的事实（注释写在这里，免得以后有人在前端「补」一遍）：
 *
 * - 过渡期内旧 Key **每请求都真做一次 scrypt 校验**（不进校验缓存），所以老业务在这
 *   24 小时里会略慢——这是刻意的；
 * - 旧 Key 过了窗口（自然到期或被提前作废）再请求，后端回的是专用文案
 *   「这把 Key 的密钥已重新生成，请使用新 Key」（业务码仍是 20010）。所以界面看到的
 *   不是「Key 不存在」；那句原文经 `dataHttp` / `ApiError.backendMsg` 原样摊出来。
 */
export function regenerateKey(keyId: string): Promise<IssuedKeyData> {
  return consoleHttp.post<IssuedKeyData>(`/console/keys/${keyId}/regenerate`)
}

/**
 * 把过渡密钥提前作废（不必等 24 小时窗口自己到期）。
 *
 * 后端实现上是**把过渡窗口的截止时刻改到过去**，并保留 `prev_key_masked` / 旧址末 5 位
 * （它们还要显示、还要给审计用）。所以界面判断窗口是否还在，只能看
 * `prev_key_expires_at > now`，不能看「有没有打码形态」。
 *
 * 幂等：没有可作废的过渡密钥（本来就没轮换过，或者已经到期 / 已作废过）时回
 * `revoked=false` 而不报错——界面上的入口只在窗口内出现，但重试、双击、两个标签页
 * 同时点都是正常操作，不该变成错误码。
 */
export function revokePreviousKey(keyId: string): Promise<KeyRevokePreviousReceipt> {
  return consoleHttp.post<KeyRevokePreviousReceipt>(`/console/keys/${keyId}/revoke-previous`)
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
