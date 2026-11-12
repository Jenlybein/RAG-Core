import type { SettingsImpact } from '../types/runtimeSettings'

/**
 * 通用影响面预览：确认判定与弹窗正文。
 *
 * 这两个函数原先住在 `utils/modelsPage.ts` 里，但它们并不只服务模型配置页——检索参数页
 * （`RetrievalPage.vue`）也用它。放在「模型配置页」名下会让下一个调用者以为这是页面私有
 * 逻辑，或者去抄一份。这里只留与具体页面无关的判定；套餐页的影响面形状不同
 * （`PlanImpact`），它的判定仍在 `utils/adminPlans.ts`。
 *
 * 一条贯穿两者的取舍：**影响面的数字一律用 `count`**。后端 `sample` 最多只放 5 个名字，
 * 它是「快速扫一眼」的材料，不是计数；拿 `sample.length` 当条件会在受影响对象更多、
 * 或样本恰好为空时漏掉弹窗——那正是这张票要防的事故。
 */

/**
 * 影响面是否值得停下来问一句。
 *
 * `quality_degraded` / `keyword_channel_lost` 两组各自判断：它们是两类不同的能力损失，
 * 任一组非零都要确认。
 */
export function shouldConfirmImpact(impact: SettingsImpact | null | undefined): boolean {
  if (impact === null || impact === undefined) return false
  return impact.quality_degraded.count > 0 || impact.keyword_channel_lost.count > 0
}

/**
 * 确认弹窗的正文：只列真正非零的组，数字用 `count`，样本作为「示例」附后。
 *
 * 返回字符串数组而不是拼好的 HTML：`ElMessageBox` 按纯文本渲染，租户名是用户可控的
 * 数据，不能被当成 HTML 插进页面。
 */
export function impactLines(impact: SettingsImpact | null | undefined): string[] {
  if (impact === null || impact === undefined) return []

  const lines: string[] = []

  if (impact.quality_degraded.count > 0) {
    lines.push(
      `${impact.quality_degraded.count} 个租户的 quality 档会少一步能力` +
        '（重排或改写被关掉）' +
        sampleHint(impact.quality_degraded.sample),
    )
  }

  if (impact.keyword_channel_lost.count > 0) {
    lines.push(
      `${impact.keyword_channel_lost.count} 个租户的 hybrid 档会失去关键词通道` +
        '（只剩向量一路召回）' +
        sampleHint(impact.keyword_channel_lost.sample),
    )
  }

  return lines
}

function sampleHint(sample: readonly string[]): string {
  if (sample.length === 0) return ''
  // 明确写「示例」：它是名单的前几个，不是全部，也不是计数。
  return `；示例：${sample.join('、')}`
}
