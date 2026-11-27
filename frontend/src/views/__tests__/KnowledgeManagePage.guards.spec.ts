/**
 * 知识库页的**模板结构守卫**（第 17 步，现场 #226 的回归）。
 *
 * ## 为什么是结构断言而不是挂载测试
 *
 * 这里守的两处都是「模板里少一个东西、页面就静默失效」的形态，而它们的失效**不报错**：
 *
 * 1. 卡片底部的「详情 →」曾经是一行**裸文字**，而它所在的按钮组带 `@click.stop`（防误触发
 *    整卡点击）。于是点它既不发请求、也不冒泡到卡片的 `@click` —— 用户看到的是「点了没反应」，
 *    控制台没有任何报错，请求也没有。这种缺陷用挂载测试能抓，但整页挂载要拖进 vue-query、
 *    router、Element Plus 与 API 层四套替身；而它真正的判据只有一句「它必须是个带 click 的
 *    按钮」，扫源码比挂载更直接，也更快。
 *
 * 2. 「一键重建」必须调 `reindexAllDocuments`（逐篇投递），不能悄悄换成「起一个整库构建」——
 *    那是另一种语义（新版本 + 收敛 + 激活），用户要的是「把这些篇重新排进队列」。
 *
 * 这两条都是**结构性**的（模板里有没有那个绑定），不是数值性的，所以断言源码是诚实的做法。
 * 反过来说：这个文件**不能**替代对 `reindexAllDocuments` 行为本身的测试——那需要一组真请求，
 * 目前没有，写在 `docs/第17步-改造报告.md` 的已知缺口里。
 */

import { describe, expect, it } from 'vitest'

// 用 Vite 的 `?raw` 读源码：`node:fs` / `__dirname` 在容器里的 `vue-tsc` 下没有类型
// （镜像里不做全量 dev 依赖安装），而 `?raw` 是打包器自己的能力，两边行为一致。
import PAGE from '../KnowledgeManagePage.vue?raw'
import CHART from '../../components/UsageChart.vue?raw'



describe('知识库卡片的操作区', () => {
  it('整卡可点 = 详情入口，且按钮组里不再有那个多余的「详情」', () => {
    const card = PAGE.indexOf('role="button"')
    expect(card).toBeGreaterThan(0)
    expect(PAGE.slice(card, card + 900)).toContain('@click="openDetail(row)"')

    // 反例（曾经的样子）：按钮组里又放了一个「详情 →」。它要么是**裸文字**（被外层
    // `@click.stop` 吞掉点击，点了没反应），要么与整卡点击完全重复——用户的原话是
    // 「一点用都没有这个按钮」。所以这里守的是「别再加回来」。
    const groupStart = PAGE.indexOf('pt-2.5')
    const groupEnd = PAGE.indexOf('</el-card>', groupStart)
    expect(groupStart).toBeGreaterThan(0)
    expect(groupEnd).toBeGreaterThan(groupStart)
    expect(PAGE.slice(groupStart, groupEnd)).not.toContain('详情')
  })

  it('「重建」调的是整库重建，不是逐篇重新投递', () => {
    const binding = PAGE.indexOf('@click="askRebuild(row)"')
    expect(binding).toBeGreaterThan(0)

    const element = PAGE.slice(binding - 700, binding + 40)
    expect(element).toContain('重建')
    // 判据是**调哪个接口**：只有产出新版本才消得掉「需重建」提示；逐篇重建写回旧版本，
    // 文档都重切了一遍、提示还挂着（第一版就是这个毛病）。
    expect(PAGE).toContain('rebuildKbConsole(')
    expect(PAGE).not.toContain('reindexAllDocuments(')
  })

  it('构建进行中禁用「重建」（并发构建由后端守，这里别给注定失败的按钮）', () => {
    const binding = PAGE.indexOf('@click="askRebuild(row)"')
    const element = PAGE.slice(binding - 700, binding + 40)

    expect(element).toContain(':disabled=')
    expect(element).toContain('rebuildingKbId')
    expect(element).toContain('row.build_progress')
  })
})

describe('用量曲线图', () => {
  it('第四本账（切块）在图上，且走**右轴**——它与前三本量纲不同', () => {
    const chart = CHART

    expect(chart).toContain("name: '切块（token）'")
    expect(chart).toContain('yAxisIndex: 1')
    // 反例：把切块堆进 `stack: 'meters'` 那根柱子——柱高会变成「次数 + token」。
    const at = chart.indexOf("name: '切块（token）'")
    const chunkSeries = chart.slice(at, at + 400)
    expect(chunkSeries).not.toContain("stack: 'meters'")
  })
})
