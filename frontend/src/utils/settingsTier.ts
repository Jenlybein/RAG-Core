import type { EmbeddingBatchHint, SettingRow, SettingValue } from '../types/runtimeSettings'

/**
 * 检索参数页的判定规则，全部放在这里，不散在组件里。
 *
 * 为什么抽成纯函数：#95 的 `utils/adminPlans.ts` 已经把「产品规则放纯函数、组件只负责
 * 何时调用」这条路走通了，本页照同一形状复用——分组顺序、可写性、发请求前的类型边界
 * 都能用 vitest 直接钉死，组件不必挂载 Vue 就能验证。
 */

/** 分组结果的形状：档位名 + 该档的行（行序与入参一致）。 */
export interface TierGroup {
  readonly tier: string
  readonly rows: SettingRow[]
}

/**
 * 按 `tier` 分组，**保留行序与档位首次出现的顺序**。
 *
 * 后端 `GET /console/settings` 已经按 locked → restart → hot 排好，并且每档都会返回
 * （哪怕是空组）。这里再分组一次是为了让页面从「行的集合」出发：空档不会在界面上
 * 留下一个没有内容的标题，而顺序仍然完全跟随后端，前端不自己排一次。
 */
export function groupByTier(rows: readonly SettingRow[]): TierGroup[] {
  const groups: TierGroup[] = []
  const byTier = new Map<string, TierGroup>()

  for (const row of rows) {
    let group = byTier.get(row.tier)
    if (group === undefined) {
      group = { tier: row.tier, rows: [] }
      byTier.set(row.tier, group)
      groups.push(group)
    }
    group.rows.push(row)
  }

  return groups
}

/**
 * 「需重启」档里**在面板上**放开编辑的那几个键（票 #224）。
 *
 * 为什么要开这个例外：``EMBEDDING_BATCH_SIZE`` 只决定「一次 HTTP 请求塞几条切片」，不改
 * 任何已落库数据的形状（``CHUNK_SIZE`` / ``EMBEDDING_DIMENSIONS`` 那种「换了就要重建」
 * 的键不在此列），而它典型的调优场景是「线上在烧钱、要立刻压下来」——让管理员去改 `.env`
 * 再重起整条链路，恰恰在最急的时候最慢。后端的白名单里对这一个键给了上界（25）与
 * 供应商事实（单批上限），复核责任仍在白名单那一侧。
 *
 * **是一份逐键的名单，不是「restart 档整体可写」这个开关**：后者等于把 #93/#96 定下的
 * 「需重启档在面板上只读」整条规则拆掉，而真正危险的是 ``CHUNK_SIZE`` / ``EMBEDDING_MODEL``
 * 这类会让新旧数据形状混在一起的键。多放开一个键，就往这里加一行。
 */
export const RESTART_PANEL_WRITABLE_KEYS: readonly string[] = ['EMBEDDING_BATCH_SIZE']

/** 面板上以可编辑控件渲染的批大小键名（提示文案与保存提示都按它认这一行）。 */
export const EMBEDDING_BATCH_SIZE_KEY = 'EMBEDDING_BATCH_SIZE'

/**
 * 这一行在面板上能不能编辑。
 *
 * 三个来源各管一段：
 *
 * 1. `locked` 档永远不给写入口。它的 `why` 里就有「能关掉鉴权」这类话，把安全边界交给
 *    一个布尔字段是错的；
 * 2. `restart` 档**只放行** {@link RESTART_PANEL_WRITABLE_KEYS} 里点名的键（票 #224）；
 *    其余仍然是「只读展示当前值与来源」，`source === 'db'` 的行标出「已写入库，待重启生效」；
 * 3. `hot` 档按后端的 `editable` 决定。
 *
 * 于是保存时提交的天然只含可写的行，「没有改动就禁用保存」不需要页面再判一次。
 */
export function isEditable(row: SettingRow): boolean {
  if (row.tier === 'hot') return row.editable
  if (row.tier === 'restart') {
    return row.editable && RESTART_PANEL_WRITABLE_KEYS.includes(row.key)
  }
  return false
}

/** 校验结果：`ok` 时带回**要发出去的值**，失败时给人话原因。 */
export type HotValueCheck =
  | { readonly ok: true; readonly value: SettingValue }
  | { readonly ok: false; readonly message: string }

/** 角色指针键前缀，与后端 `app/core/model_roles.py` 的 `POINTER_KEY_PREFIX` 逐字一致。 */
export const ROLE_POINTER_PREFIX = 'model.role.'

/** 这一行是不是「角色指向」指针（它的值是 `model_configs` 的 id，或空串 = 不指向）。 */
export function isRolePointerKey(key: string): boolean {
  return key.startsWith(ROLE_POINTER_PREFIX)
}

/** 一行输入控件对应的值类型。 */
export type SettingValueKind = 'boolean' | 'number' | 'string'

/**
 * 推断这一行期望的输入种类。
 *
 * 依据是**有效值的类型**而不是键名的类型表：白名单的类型真相在后端
 * `SETTING_SPECS` / `Settings.model_fields`，前端再抄一张「哪个键是数字」的表，加一个键
 * 就要改两处。角色指针的值为 `null`（还不指向任何一条），但它的输入是一段 id 文本，
 * 所以单独按字符串处理。
 *
 * 导出而不是留在页面里：草稿用哪种容器、模板里渲染哪个控件、发请求前的校验，
 * 这三处必须认同同一个判断，页面里再写一份 `typeof` 分支就会慢慢漂移。
 */
export function kindOf(row: SettingRow): SettingValueKind {
  if (isRolePointerKey(row.key)) return 'string'
  if (typeof row.value === 'boolean') return 'boolean'
  if (typeof row.value === 'number') return 'number'
  return 'string'
}

/**
 * 发请求前的输入校验：拦住「空着没填」「NaN」「负值」「越界」这类一眼可见的错误，
 * 省一次 20015 往返。
 *
 * 边界**不抄**：下界 / 上界来自这一行的 `minimum` / `maximum`（后端白名单声明的，
 * 随行返回）。前端自己写一张「哪个键是数字、下界是多少」的表，加一个键就要改两处，
 * 而且两张表迟早在某一次改动上分叉——那时候页面会显示「保存成功」而后端其实拒了。
 *
 * 刻意**不做**宽松转换：`"true"` 不变成 `true`、`"9"` 不变成 `9`。pydantic 的宽松模式
 * 会把它们收下，但那是一次「看起来保存成功、其实值变了」的静默改写。
 */
export function validateHotValue(row: SettingRow, input: unknown): HotValueCheck {
  if (!isEditable(row)) {
    return { ok: false, message: '这一档不可写：面板不提供入口，只能由部署方改 .env 后重启' }
  }

  const kind = kindOf(row)

  if (kind === 'boolean') {
    if (typeof input !== 'boolean') {
      return { ok: false, message: '这一项只能是开 / 关两种状态' }
    }
    return { ok: true, value: input }
  }

  if (kind === 'number') {
    if (typeof input !== 'number' || !Number.isFinite(input)) {
      return { ok: false, message: '请输入一个有效数字' }
    }
    if (input < 0) {
      return { ok: false, message: '不能是负数' }
    }
    // 边界检查带上「当前值」：长清单里只报「越界」不够，管理员要知道自己填的是什么。
    if (row.minimum !== null && input < row.minimum) {
      return { ok: false, message: `必须 ≥ ${row.minimum}（当前 ${input}）` }
    }
    if (row.maximum !== null && input > row.maximum) {
      return { ok: false, message: `必须 ≤ ${row.maximum}（当前 ${input}）` }
    }
    return { ok: true, value: input }
  }

  if (typeof input !== 'string') {
    return { ok: false, message: '这一项必须是文本' }
  }
  return { ok: true, value: input }
}

/** 「需重启」档一旦可写，旁边就必须写清「保存不是生效」（票 #224 的验收原话）。 */
export const RESTART_WORKER_NOTICE = '保存后需重启 Worker 生效'

/**
 * 可编辑的「需重启」行旁边那句提示；其余行为 `null`（没有这句就说明不需要提醒）。
 *
 * 只对 {@link RESTART_PANEL_WRITABLE_KEYS} 里放开的行返回：只读的「需重启」行根本没有
 * 保存动作，在它们旁边挂一句「保存后需重启」只会让读者以为哪里能点。
 */
export function restartNoticeOf(row: SettingRow): string | null {
  return isEditable(row) && row.tier === 'restart' ? RESTART_WORKER_NOTICE : null
}

/**
 * 嵌入批大小那一行的旁注：「当前模型 X（单批上限 Y），建议 ≤ Y」。
 *
 * X / Y 都由后端给（`embedding_batch`）：模型→单批上限是供应商事实，抄到前端就会漂移。
 * 非这一行返回 `null`；这一行但后端没给素材（旧版后端 / 契约变更）时退化成 `null`，
 * 页面于是少一句提示而不是显示一句「当前模型 （单批上限 undefined）」。
 */
export function embeddingBatchHintOf(
  row: SettingRow,
  hint: EmbeddingBatchHint | null | undefined,
): string | null {
  if (row.key !== EMBEDDING_BATCH_SIZE_KEY) return null
  if (hint === null || hint === undefined || hint.model === '' || hint.limit <= 0) return null
  return `当前模型 ${hint.model}（单批上限 ${hint.limit}），建议 ≤ ${hint.limit}`
}
