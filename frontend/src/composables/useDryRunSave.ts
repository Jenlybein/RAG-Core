import { ElMessageBox } from 'element-plus'
import { ref, type Ref, type VNode } from 'vue'

/**
 * 「先试算、有影响才确认、确认后再真写」的两步保存。
 *
 * 后台有两处 PATCH 是同一个形状（套餐页 `updatePlan`、检索参数页 `updateSettings`）：
 * 第一次带 `dry_run=true` 只拿影响面，第二次带 `dry_run=false` 才落库。这段顺序、以及
 * 「确认框被取消时不算失败」的处理，以前在两个页面各写了一遍，这里收成一处。
 *
 * **弹窗文案、以及「什么影响面值得停下来问」的判定都留在页面**：两个接口的 impact 形状
 * 不同（套餐是「多少租户会超库数」，检索参数是「多少租户的档位会少一步能力」），
 * 判定与措辞放进一个通用函数只会逼着它长出两套分支。本组合式函数只负责：
 *
 * 1. 先发试算；
 * 2. 交给页面的 `formatImpact` 决定要不要确认、确认框里写什么；
 * 3. 用户确认后发真写；取消则**什么都不落库**，并如实报告「cancelled」；
 * 4. 全程维护 `saving`（试算、弹窗与真写都算「保存中」）。
 */
export interface ImpactConfirmCopy {
  /** 确认框标题，由页面写。 */
  title: string
  /** 确认框正文；字符串或 VNode 都行，由页面拼好。 */
  message: string | VNode
}

/** 页面侧的影响面格式化：返回 null 表示「这次不需要确认」。 */
export type FormatImpact<TImpact> = (impact: TImpact | null) => ImpactConfirmCopy | null

export interface UseDryRunSave<TImpact> {
  /** 两步流程进行中；模板用它绑按钮的 `:loading`。 */
  readonly saving: Ref<boolean>
  /**
   * 执行一次两步保存。
   *
   * `submit` 由页面在调用时给出，因为它通常要捕获「这次点保存时的草稿快照」
   * （例如套餐的 patch、检索参数的 values）。把快照交给本函数而不是在两次请求里
   * 重新计算，预览与落库才会是同一份改动。
   */
  save<TResult extends { impact: TImpact | null }>(
    submit: (dryRun: boolean) => Promise<TResult>,
  ): Promise<DryRunSaveOutcome<TResult>>
}

/** 保存的三种结局；「取消确认」不是错误，必须与请求失败分开。 */
export type DryRunSaveOutcome<TResult> =
  | { readonly status: 'saved'; readonly result: TResult }
  | { readonly status: 'cancelled' }
  | { readonly status: 'failed'; readonly error: unknown }

export interface DryRunSaveOptions<TImpact> {
  formatImpact: FormatImpact<TImpact>
}

export function useDryRunSave<TImpact>(
  options: DryRunSaveOptions<TImpact>,
): UseDryRunSave<TImpact> {
  const saving = ref(false)

  async function save<TResult extends { impact: TImpact | null }>(
    submit: (dryRun: boolean) => Promise<TResult>,
  ): Promise<DryRunSaveOutcome<TResult>> {
    saving.value = true
    try {
      // 第一步：只试算。显式传 true，不依赖后端 `dry_run` 的缺省值——
      // 「这是试算」要写在代码里，而不是写在后端的某个默认值里。
      const preview = await submit(true)

      const copy = options.formatImpact(preview.impact)
      if (copy !== null) {
        try {
          await ElMessageBox.confirm(copy.message, copy.title, {
            type: 'warning',
            confirmButtonText: '确认保存',
            cancelButtonText: '取消',
          })
        } catch {
          // ElMessageBox 的 reject 是用户点了取消，不是请求失败：
          // 不落库、不弹错误，也不能让页面以为「保存坏了」。
          return { status: 'cancelled' }
        }
      }

      // 第二步：真写。
      return { status: 'saved', result: await submit(false) }
    } catch (error) {
      return { status: 'failed', error }
    } finally {
      saving.value = false
    }
  }

  return { saving, save }
}
