import type { SettingRow, SettingValue } from '../types/runtimeSettings'

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
 * 这一行能不能编辑。
 *
 * 不直接信任后端的 `editable`：它表达的是「PATCH 会不会接受它」（restart 档是 true，
 * 因为写库本身合法，只是要重启才生效），而页面还要额外守一条「locked 档绝不提供写入口」
 * ——那一档的 `why` 里就有「能关掉鉴权」，把安全边界交给一个布尔字段是错的。restart 档
 * 允许保存并显示「待重启」，这正是后端「把意图记下来」的设计。
 */
export function isEditable(row: SettingRow): boolean {
  return row.tier !== 'locked' && row.editable
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

type Kind = 'boolean' | 'number' | 'string'

/**
 * 推断这一行期望的输入种类。
 *
 * 依据是**有效值的类型**而不是键名的类型表：白名单的类型真相在后端
 * `SETTING_SPECS` / `Settings.model_fields`，前端再抄一张「哪个键是数字」的表，加一个键
 * 就要改两处。角色指针的值为 `null`（还不指向任何一条），但它的输入是一段 id 文本，
 * 所以单独按字符串处理。
 */
function kindOf(row: SettingRow): Kind {
  if (isRolePointerKey(row.key)) return 'string'
  if (typeof row.value === 'boolean') return 'boolean'
  if (typeof row.value === 'number') return 'number'
  return 'string'
}

/**
 * 发请求前的输入校验：拦住「空着没填」「NaN」「负值」这类一眼可见的错误，省一次
 * 20015 往返。
 *
 * 刻意**不做**以下两件事：
 *
 * 1. **不复制后端的下界表**（`TOP_K ≥ 1`、`CONSOLE_SESSION_TTL ≥ 60` …）。那张表在后端
 *    白名单里逐键声明，前端抄一份就会漂移；这里只拒绝负值（没有任何热更键允许负数），
 *    其余下界由后端判定并在 20015 的原文里说明。
 * 2. **不做宽松转换**：`"true"` 不变成 `true`、`"9"` 不变成 `9`。pydantic 的宽松模式
 *    会把它们收下，但那是一次「看起来保存成功、其实值变了」的静默改写。
 */
export function validateHotValue(row: SettingRow, input: unknown): HotValueCheck {
  if (!isEditable(row)) {
    return { ok: false, message: '这一档不可写：只能由部署方改 .env，面板不提供入口' }
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
    return { ok: true, value: input }
  }

  if (typeof input !== 'string') {
    return { ok: false, message: '这一项必须是文本' }
  }
  return { ok: true, value: input }
}
