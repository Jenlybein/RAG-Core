/**
 * 「过渡 Key」的显示规则：窗口还在不在、还剩多久、到几点、tooltip 怎么说。
 *
 * 为什么必须是纯函数：这些数字与句子回答的是**同一个问题**——「旧 Key 还能不能继续过校验，
 * 到什么时候为止」。把它拆在模板里写，就出现两处各自算一遍 24 小时窗口的机会：一处说
 * 「还剩 3 小时」、另一处说「至 14:05」，两个答案对不上时用户不知道该信哪个。
 *
 * 时间基准由调用方传进来（`nowMs`），函数内部不读 `Date.now()`：这样用例能钉住边界
 * （59 分、刚好 24 小时、已过期），而页面照常在渲染时取当下时刻。
 *
 * ## 判据只看截止时刻，不看「有没有打码形态」
 *
 * 后端把提前作废实现成「把过渡窗口的截止时刻改到过去」，而 `prev_key_masked` /
 * `prev_key_tail` **会保留**（它还要继续给「旧址末 5 位」与审计用）。所以
 * `prev_key_masked !== null` 只说明「轮换过、旧址还在记录里」，不说明现在还能用；
 * 唯一作数的判据是 `prev_key_expires_at > now`。窗口结束后界面上不该再出现任何提示——
 * 用户要的是「现在还能不能过校验」，不是一份状态变更史。
 *
 * ## 两条后端已经保证的事实（前端不重复实现，但注释要说对）
 *
 * - 过渡期内旧 Key **每请求都要真做一次 scrypt 校验**（它不进校验缓存），所以老业务在
 *   这 24 小时里会略慢一点。这是有意的：缓存里放一份「正在退役的凭证」等于把窗口的
 *   边界藏起来。
 * - 旧 Key 过了窗口（自然到期或被提前作废）再请求，后端回的是专用文案
 *   「这把 Key 的密钥已重新生成，请使用新 Key」（业务码仍是 20010）。所以调试台看到的
 *   不是「Key 不存在」，而是「该换新 Key 了」——那句原文会被原样摊在输入框下面。
 */

/** 过渡窗口的长度；与后端 `api_key_service.PREV_KEY_TTL` 同源，仅用于「不超过 24 小时」的兜底。 */
export const PREV_KEY_WINDOW_MS = 24 * 60 * 60 * 1000

const MINUTE_MS = 60 * 1000
const HOUR_MS = 60 * MINUTE_MS

/** 与 `utils/datetime.ts` 同一条纪律：解析不了就不装作知道，交给调用方显示原文/省略。 */
function parseDeadline(value: string): number | null {
  const millis = new Date(value).getTime()
  return Number.isNaN(millis) ? null : millis
}

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 过渡窗口是否**此刻还开着**。
 *
 * 这是 `ⓘ` 与「作废旧 Key」入口的唯一判据：作废之后（或自然到期之后）后端仍保留
 * `prev_key_masked`，只看它会让两个入口在窗口结束后继续挂着——点下去只会得到
 * 「已经没有可作废的过渡 Key」，等于留了一扇打不开的门。
 */
export function isTransitionActive(expiresAt: string | null, nowMs: number): boolean {
  if (expiresAt === null || expiresAt.trim() === '') return false
  const deadline = parseDeadline(expiresAt)
  return deadline !== null && deadline > nowMs
}

/**
 * 截止时刻的短形态 `MM-DD HH:mm`（本地时间）。
 *
 * 只给 tooltip 用：那里空间有限，而「哪一年」不是问题——过渡窗口只有 24 小时，跨年也不会
 * 造成误读。完整日期留给审计页，这里显示年份只会挤掉真正有用的小时与分钟。
 */
export function formatTransitionDeadline(expiresAt: string | null): string {
  if (expiresAt === null || expiresAt.trim() === '') return '—'
  const millis = parseDeadline(expiresAt)
  if (millis === null) return expiresAt
  const date = new Date(millis)
  return (
    `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}

/**
 * 剩余时长：`N 小时 M 分` / `不足 1 小时` / `已过期`。
 *
 * 不足 1 小时时不显示分钟数：用户此刻要做的是「把业务换过来」，197 分钟和 42 分钟的行动
 * 价值一样；而「不足 1 小时」这句话的紧迫感比任何精确分钟数都清楚。
 * 上限压在 24 小时：浏览器钟比服务端慢时，按截止时刻直接减会算出「25 小时 12 分」，
 * 而窗口本身不可能长于 24 小时——那个数字只会让人以为窗口被延长了。
 */
export function formatTransitionRemaining(expiresAt: string | null, nowMs: number): string {
  if (expiresAt === null || expiresAt.trim() === '') return '—'
  const deadline = parseDeadline(expiresAt)
  if (deadline === null) return '—'
  const remaining = Math.min(deadline - nowMs, PREV_KEY_WINDOW_MS)
  if (remaining <= 0) return '已过期'
  if (remaining < HOUR_MS) return '不足 1 小时'
  const hours = Math.floor(remaining / HOUR_MS)
  const minutes = Math.floor((remaining % HOUR_MS) / MINUTE_MS)
  return `${hours} 小时 ${minutes} 分`
}

/**
 * 状态徽标旁那个 `ⓘ` 的 tooltip 文案。
 *
 * 两句话各管一件事：第一句是**事实**（哪把过渡 Key、还能用到几点、还剩多久），第二句是
 * **边界**（它只用来平滑切换，过期或被作废后不再被接受）。只写事实，用户会以为这是一把
 * 长期可用的第二把 Key；只写边界，用户不知道还剩多久、该什么时候动手。
 *
 * 窗口已关时返回空串：调用方（`v-if` 与 tooltip）都只在窗口内挂这个图标，函数本身再守
 * 一道，避免「图标没了、tooltip 还留着一句过期文案」这种只可能由重构制造出来的状态。
 */
export function prevKeyTooltip(
  prevKeyMasked: string,
  expiresAt: string | null,
  nowMs: number,
): string {
  if (!isTransitionActive(expiresAt, nowMs)) return ''
  const deadline = formatTransitionDeadline(expiresAt)
  const remaining = formatTransitionRemaining(expiresAt, nowMs)
  // 「不足 1 小时」是一句话，不是数字短语：写成「还剩 不足 1 小时」会在中文里断成两截。
  const remainingCopy = remaining === '不足 1 小时' ? '还剩不足 1 小时' : `还剩 ${remaining}`
  return (
    `过渡 Key ${prevKeyMasked} 仍可用，至 ${deadline}（${remainingCopy}）。` +
    '它只用于让老业务平顺切换，过期或被作废后不再被接受。'
  )
}
