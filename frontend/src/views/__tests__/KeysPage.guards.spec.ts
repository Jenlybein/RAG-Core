/**
 * `/keys` 的两条新流程守卫：**获取 Key / 重新生成** 与 **过渡 Key 提示 / 提前作废**。
 *
 * ## 为什么是结构断言而不是挂载测试
 *
 * 这里守的判据都是「模板里少一个条件、页面就静默说错话」的形态，而且失效不报错：
 *
 * 1. 「获取 Key」只能是行内入口——空状态页没有「已有一把可以重新生成」可言，加一个
 *    页面级入口会让第一次来的人先点一个不该存在的动作；
 * 2. 两态弹窗里，展示态**不能再出现**「重新生成」：这时点它会轮换掉正在展示、还没复制的
 *    那一串；
 * 3. `ⓘ` 与「作废旧 Key」的判据是 `prev_key_expires_at > now`，**不是**
 *    `prev_key_masked !== null`——后端作废时把窗口关到过去却保留打码形态，判据写错会让
 *    两个入口在窗口结束后继续挂着。
 *
 * 整页挂载要拖进 vue-query、router、Element Plus 与 API 层四套替身，而判据只有两三句话，
 * 扫源码更直接。接口形状的测试在 `services/__tests__/keyService.spec.ts`。
 */

import { describe, expect, it } from 'vitest'

import PAGE from '../KeysPage.vue?raw'
import DIALOG from '../../components/OneTimeKeyDialog.vue?raw'
import SERVICE from '../../services/keyService.ts?raw'
import TYPES from '../../types/key.ts?raw'

/** 从 `from` 处开始、到 `to` 之前（用来把一段模板单独切出来看）。 */
function slice(source: string, from: string, to: string): string {
  const start = source.indexOf(from)
  const end = source.indexOf(to, start)
  expect(start).toBeGreaterThan(-1)
  expect(end).toBeGreaterThan(start)
  return source.slice(start, end)
}

describe('「获取 Key」入口', () => {
  it('只出现在行内操作区，不带页面级入口', () => {
    const binding = PAGE.indexOf('@click="openGetKey(row)"')
    expect(binding).toBeGreaterThan(0)
    // 同一段的 300 字内必须有「获取 Key」：入口与它的绑定不能被拆到两处。
    expect(PAGE.slice(binding - 200, binding + 200)).toContain('获取 Key')
    // 反例：页面级入口（没有 row）。那会让空状态页也出现「获取」——空状态只有「签发新 Key」。
    expect(PAGE).not.toContain('@click="openGetKey()"')
    expect(PAGE).not.toContain('openGetKey()')
  })

  it('空状态继续用「签发新 Key」', () => {
    const empty = slice(PAGE, "keys.length === 0", '</div>')
    expect(empty).toContain('签发新 Key')
    expect(empty).toContain('openIssue')
    expect(empty).not.toContain('获取 Key')
  })

  it('重新生成走 regenerateKey，签发仍走 createKey（两个端点不能混用）', () => {
    expect(PAGE).toContain('regenerateKey(')
    expect(PAGE).toContain('createKey(')
    // 服务层的路径写死：轮换不是「再签发一把」，它的 key_id / 配置 / 白名单都不变。
    expect(SERVICE).toContain('regenerate')
    expect(SERVICE).toContain(`/console/keys/\${keyId}/regenerate`)
  })

  it('签发的事前警示挂在真正发请求的「签发」提交上', () => {
    // 页面顶部的「签发新 Key」只打开配置表单（此时还没有任何请求可发）。
    const openIssue = slice(PAGE, 'function openIssue', 'function openEdit')
    expect(openIssue).toContain('editorOpen.value = true')
    expect(openIssue).not.toContain('openKeyDialog')
    // 表单提交时先弹警示；确认那一侧才调 createKey，成功后才切展示态。
    const save = slice(PAGE, 'async function save', '// —— 「获取 Key」')
    expect(save).toContain("openKeyDialog('issue', null)")
    const confirm = slice(PAGE, 'async function confirmKeyDialog', '// —— 提前作废')
    expect(confirm).toContain('keyDialogMode.value === \'issue\'')
    expect(confirm).toContain('createKey(')
    expect(confirm).toContain('keyDialogResult.value = created')
  })
})

describe('过渡 Key 的提示与作废入口', () => {
  it('`ⓘ` 与「作废旧 Key」的判据是 `transitionActive(row)`（时间比较），不是 masked 非空', () => {
    // 两个入口同一个条件：窗口还在才出现。
    expect(PAGE.match(/v-if="transitionActive\(row\)"/g)?.length).toBe(2)
    expect(PAGE).toContain('ⓘ')
    // 反例：用 masked 判空。作废/到期后 masked 仍会保留，那会让两个入口一直挂着。
    expect(PAGE).not.toContain('v-if="row.prev_key_masked !== null"')
    // 纯函数里判的是截止时刻；`prevKeyTooltip` 在窗口外返回空串。
    expect(PAGE).toContain("import { isTransitionActive, prevKeyTooltip }")
  })

  it('行内不再写「旧 Key 已失效」这类追认文案', () => {
    // 注释里说明「不要写」是允许的；这里守的是它不能作为**渲染文字**出现在模板里。
    expect(PAGE).not.toContain('>旧 Key 已失效')
    expect(PAGE).not.toContain('>过渡期已结束')
  })

  it('作废：危险确认 + 调 revokePreviousKey；revoked=false 只回一句 info', () => {
    const revoke = slice(PAGE, 'v-model="revokeOpen"', '@confirm="confirmRevoke"')
    expect(revoke).toContain('title="作废旧 Key"')
    expect(revoke).toContain('confirm-text="作废"')
    expect(revoke).toContain('danger')
    expect(PAGE).toContain('revokePreviousKey(')
    expect(PAGE).toContain('await queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY })')
    // 幂等答案（本来就没有 / 已到期 / 已作废过）不是错误：info，不是 error。
    expect(PAGE).toContain('receipt.revoked')
    expect(PAGE).toContain('这把 Key 已经没有可作废的过渡 Key')
  })
})

describe('两态弹窗（OneTimeKeyDialog）', () => {
  it('警示态与展示态都在同一个组件、同一段流程里', () => {
    expect(PAGE).toContain('<OneTimeKeyDialog')
    expect(PAGE).toContain(':mode="keyDialogMode"')
    expect(PAGE).toContain(':plaintext="keyDialogResult?.key_plaintext ?? null"')
    expect(DIALOG).toContain('data-state="warning"')
    expect(DIALOG).toContain('data-state="reveal"')
  })

  it('警示态正文照抄，且签发模式不出现过渡 Key / 24 小时那些段落', () => {
    const warning = slice(DIALOG, 'data-state="warning"', 'data-state="reveal"')
    // 第一段两边都有：签发同样只显示一次。
    expect(warning).toContain(
      '这个 Key 只会显示这一次。关闭之后，平台不再保存它的明文，也无法再为你调取——请现在复制并妥善保管。',
    )
    // 过渡期那三段与勾选只在 regenerate 分支里。
    const regenerateOnly = slice(warning, `v-if="props.mode === 'regenerate'"`, '</template>')
    expect(regenerateOnly).toContain(
      '重新生成后，当前 Key 会变成过渡 Key：它仍然可用 24 小时，期间新旧两把都能通过校验，方便你把业务换过来。想让它提前失效，随时可以在列表里作废。',
    )
    expect(regenerateOnly).toContain(
      '过渡期只保留最近一把：24 小时内再点一次「获取 Key」，上一把过渡 Key 会立刻作废。',
    )
    expect(regenerateOnly).toContain('我知道当前 Key 会在 24 小时后失效')
    // 签发路径的第一段里不能出现这些词：签发不影响任何旧 Key。
    const firstParagraph = warning.slice(0, warning.indexOf('props.mode'))
    expect(firstParagraph).not.toContain('过渡')
    expect(firstParagraph).not.toContain('24 小时')
    // 勾选框只在重新生成模式需要；签发不需要额外勾选。
    expect(warning).toContain('v-if="props.mode === \'regenerate\'"')
  })

  it('按钮文案随模式变化；重新生成必须勾选后才可用', () => {
    expect(DIALOG).toContain("'重新生成新 Key' : '签发新 Key'")
    expect(DIALOG).toContain('!acknowledging.value')
    expect(DIALOG).toContain(':disabled="confirmDisabled"')
  })

  it('展示态：只读 textarea、全选、复制、2 秒后回到「复制」', () => {
    // 上半句是 computed（两种模式各说各的），正文里用 `{{ leadText }}`。
    expect(DIALOG).toContain("'已重新生成。下面是它唯一一次的展示：'")
    const reveal = slice(DIALOG, 'data-state="reveal"', '<template #footer>')
    expect(reveal).toContain('{{ leadText }}')
    expect(reveal).toContain(':model-value="props.plaintext ?? \'\'"')
    expect(reveal).toContain('readonly')
    expect(reveal).toContain('@focus="selectAll"')
    expect(reveal).toContain("{{ copied ? '已复制' : '复制' }}")
    expect(reveal).toContain('关闭后无法再次查看。')
    // 「重新生成」不能在展示态出现：手一滑就把还没复制的 Key 轮换掉。
    expect(reveal).not.toContain('重新生成')
    // 2 秒回退：按钮不能永久停在「已复制」。
    expect(DIALOG).toContain('2000')
    expect(DIALOG).toContain('navigator.clipboard.writeText')
    expect(DIALOG).toContain('复制失败，请手动全选复制')
  })

  it('展示态不能点遮罩 / ESC 关闭，且底部只剩关闭（重新生成被隐藏）', () => {
    expect(DIALOG).toContain(':close-on-click-modal="!showing')
    expect(DIALOG).toContain(':close-on-press-escape="!showing')
    expect(DIALOG).toContain(':show-close="!showing')
    const footer = slice(DIALOG, '<template #footer>', '</el-dialog>')
    expect(footer).toContain('v-if="!showing"')
    // 展示态的分支里没有「重新生成」按钮。
    const revealBranch = footer.slice(footer.indexOf('v-else'))
    expect(revealBranch).not.toContain('重新生成')
  })

  it('「去调试台试用」保留，并写进调试台读取的 sessionStorage 键', () => {
    expect(PAGE).toContain('去调试台试用')
    expect(PAGE).toContain('writeRetrieveApiKey(')
  })
})

describe('契约字段', () => {
  it('KeyData 补上三个过渡字段', () => {
    expect(TYPES).toContain('prev_key_masked: string | null')
    expect(TYPES).toContain('prev_key_expires_at: string | null')
    expect(TYPES).toContain('rotated_at: string | null')
  })
})
