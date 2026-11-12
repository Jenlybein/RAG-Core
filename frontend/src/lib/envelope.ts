import { z } from 'zod'

/** 两条身份通道的代号；文案与后续的会话处理都按它分流，绝不合流。 */
export type Face = 'data' | 'console'

/**
 * 业务失败的统一形状。
 *
 * `code` 必须留在错误对象上：页面靠它区分「换 Key」「去登录」「等额度」，
 * 只留一句 message 就无法给出可操作的下一步。
 */
export class ApiError extends Error {
  readonly code: number
  readonly face: Face
  /** 后端原文，排障时与日志对得上；界面展示用 `message`（人话）。 */
  readonly backendMsg: string

  constructor(code: number, face: Face, backendMsg: string, messageOverride?: string) {
    super(messageOverride ?? humanMessage(code, face, backendMsg))
    this.name = 'ApiError'
    this.code = code
    this.face = face
    this.backendMsg = backendMsg
  }
}

/**
 * 后端所有业务失败都是 HTTP 200 + `code!==0`（ADR-0007），所以判断只能看信封里的 code。
 * 这里用 zod 先验形状：代理返回 HTML、FastAPI 的 422 detail 都不该被当成「成功但 data 为空」。
 */
const envelopeSchema = z.object({
  code: z.number(),
  msg: z.string().nullish(),
  data: z.unknown().optional(),
})

export function unwrapEnvelope<T>(body: unknown, face: Face): T {
  const parsed = envelopeSchema.safeParse(body)
  if (!parsed.success) {
    throw new ApiError(
      -1,
      face,
      '',
      '后端响应不是 {code,msg,data} 信封：多半是代理或网关返回了非业务响应，先看 Vite 代理与后端日志',
    )
  }

  if (parsed.data.code === 0) {
    return parsed.data.data as T
  }

  throw new ApiError(parsed.data.code, face, parsed.data.msg ?? '')
}

/**
 * 只有控制面的 20010 才意味着「会话没了」。数据面的 20010 是 Key 无效，
 * 拿它去清控制台会话会把一个本来有效的登录态误删。
 */
export function isConsoleSessionExpired(code: number): boolean {
  return code === 20010
}

const CODE_COPY: Record<number, string | Record<Face, string>> = {
  20002: '参数不对：按提示检查填写内容后再提交',
  20005: '操作太频繁：稍等一会儿再试',
  20008: '知识库不存在，或不在当前 Key 的密钥配置白名单里',
  // 同一个码、两套下一步：数据面换 Key，控制面重新登录。
  20010: {
    data: '这把 API Key 无效或已停用：去 Key 管理页换一把',
    console: '未登录或会话已过期：请先登录控制台',
  },
  // 20011 是「身份没权限 / 缺防跨站头」，故意不提登录：提了就会被当成掉线。
  20011: '当前身份没有这个权限（写请求还可能是缺少防跨站请求头）',
  20013: '当前 Key 的密钥配置没有授权这项能力：去 Key 管理页改配置',
  20014: '配额已用尽：等额度重置，或去套餐 / 密钥配置里调整上限',
  20015: '参数非法，或几项配置之间互相矛盾：检查后再保存',
  20016: '对象不存在或已被删除',
  20017: '套餐不存在',
  20018: '套餐配置非法',
  20019: '套餐仍被账号引用：先把账号换到别的套餐再删',
  // 反馈（#105）：三个码的下一步完全不同，各说各的，别合成一句「失败」。
  20020: '评分没存下来，请稍后重试',
  20021: '这次检索记录不存在或不属于当前账号',
  20022: '分数需在 1~5 之间',
  20023: '文档解析失败：检查文件内容后重新上传',
  20024: '文档内容为空：换一份有内容的文件',
  20025: '文档仍在处理中：等它进入终态再操作',
  20026: '文件过大：压缩或拆分后再传',
  20027: '重建需要 force：确认要丢弃现有索引后重试',
  20028: '该格式不被这个知识库接收：换文件或换库',
  50002: '检索失败：检查向量库 / 关键词索引是否可用，或稍后重试',
}

function humanMessage(code: number, face: Face, backendMsg: string): string {
  // 数据面的 20010 有**四种**原因（缺 token / Key 不存在 / Key 停用 / 服务端还在开发身份
  // 模式），只有服务端知道是哪一种，而四种的下一步完全不同。通用文案在这里会盖掉唯一
  // 有用的那句话——真实反馈就是这样来的：服务端说「开发身份模式只认占位 Key」，界面却说
  // 「去 Key 管理页换一把」，于是用户反复重签 Key，永远修不好。
  //
  // 所以：数据面 20010 **有后端原文就照用**；没原文才退回下面那句通用文案。
  // 控制面不适用（那边 20010 恒等于「会话没了」，与后端原文无关）。
  if (code === 20010 && face === 'data' && backendMsg !== '') return backendMsg

  const copy = CODE_COPY[code]
  if (copy !== undefined) {
    return typeof copy === 'string' ? copy : copy[face]
  }
  // 没登记的码至少把后端原文露出来，而不是笼统的「请求失败」。
  return backendMsg || `请求失败（业务码 ${code}）`
}
