/**
 * 评分控件用到的纯函数（#106）。
 *
 * 三种失败各给一句人话是票面的硬要求：把它们合成一句「失败」，调用方只会一遍遍重试，
 * 而三种失败的下一步完全不同（改分数 / 换一次检索 / 稍后再来）。
 *
 * 这里刻意**不**从 `envelope.ts` 反查文案：`ApiError` 已经带 `code`，而在页面上还要
 * 判断「这个码是不是反馈的码」。把映射收在本文件一处，测试就能直接喂数字、钉死文案，
 * 不需要先构造一个 Axios 错误。
 */

export type FeedbackFace = 'data'

/** 业务码 → 给使用者的下一步。只列反馈接口会返回的三个码。 */
const FEEDBACK_COPY: Record<number, string> = {
  20020: '评分没存下来，请稍后重试',
  20021: '这次检索记录不存在或不属于当前账号',
  20022: '分数需在 1~5 之间',
}

/** 越界分数在**前端**就挡掉：发一个必被 20022 拒的请求只是白跑一趟。 */
export function isValidScore(score: number): boolean {
  return Number.isInteger(score) && score >= 1 && score <= 5
}

/** 1~5 → 星星文案。超出范围回空串，不伪造一个「0 星」的说法。 */
export function scoreStars(score: number): string {
  if (!isValidScore(score)) return ''
  return '★'.repeat(score) + '☆'.repeat(5 - score)
}

/**
 * 错误码 → 一句人话。
 *
 * 反馈之外的码（例如 20010 Key 失效）不该被这一层改写：返回空串，让调用方沿用
 * `errorText()` 的通用文案。否则「换个 Key」会被说成「评分没存下来」，那是误导。
 */
export function feedbackErrorCopy(code: number): string {
  return FEEDBACK_COPY[code] ?? ''
}
