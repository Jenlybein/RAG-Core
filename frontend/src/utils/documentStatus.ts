/**
 * 文档状态四态的**唯一**前端来源（与后端 `app/utils/status.py` 一一对应）。
 *
 * 为什么要有这个文件：三态时代那几个数字散在五处——徽章的颜色表、轮询条件、
 * 上传结果行、「版本弹窗忙不忙」、列表里的按钮开关。它们各自写 `status === 3`，
 * 于是「加一个状态」等于「改五处、漏一处就静默显示错话」。四态（ADR-0034 决策一）
 * 之后这里集中回答三个问题：
 *
 * - 这个数字是哪一态（`DOCUMENT_STATUS`）；
 * - 它**还没跑完**吗（`isPendingDocument`：轮询与在途提示读它）；
 * - 它**此刻不能操作**吗（`isDocumentBusy`：只有「索引中」可能是真在跑）。
 *
 * ## 为什么「还没跑完」和「不能操作」是两个不同的判断
 *
 * 排队中（4）与索引中（3）都还没跑完——都要继续轮询；但只有索引中可能有一个 Worker
 * 正在写这篇的切片，也只有它会被后端以 20025 拒掉。排队中**可以删除、可以重新提交、
 * 可以回滚**（撤回 / 替换一条还没被执行的意图）。把它们并成一个 `isPending` 去禁按钮，
 * 排队就会变成囚笼——那正是这次要修的东西。
 */

/** 四态取值。顺序与后端 `DocumentStatus` 一致，改这里必须同步 `app/utils/status.py`。 */
export const DOCUMENT_STATUS = {
  /** 成功：这一稿的切片已按生效版本建好。 */
  SUCCESS: 1,
  /** 失败：试过没成，原因在 `error_message` 里。 */
  FAILED: 2,
  /** 索引中：**租约有效**才算真的在跑（前端看不到租约，只能保守按状态判）。 */
  PROCESSING: 3,
  /** 排队中：已收下、没有 Worker 接，可以删除 / 重新提交。 */
  QUEUED: 4,
} as const

const PENDING: ReadonlySet<number> = new Set([
  DOCUMENT_STATUS.PROCESSING,
  DOCUMENT_STATUS.QUEUED,
])

/**
 * 这份文档是否**还没跑完**（排队中或索引中）。
 *
 * 轮询条件与「这页还有在途文档」的提示都读它：只看「等于 3」会让刚上传（排队中）的
 * 文档永远等不到刷新——用户看到「排队中」一直不动，而它其实早就跑完了。
 */
export function isPendingDocument(status: number): boolean {
  return PENDING.has(status)
}

/**
 * 这份文档此刻是否**可能有人正在写它的切片**。
 *
 * 只有「索引中」算：这是后端 20025 守卫的口径（租约有效才拒）。排队中恒为 `false`，
 * 于是删除 / 重建 / 回滚按钮在排队时照样给出。
 */
export function isDocumentBusy(status: number): boolean {
  return status === DOCUMENT_STATUS.PROCESSING
}
