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
  it('「详情」必须是一个真按钮，并显式打开详情弹窗', () => {
    // 反例（曾经的写法）：按钮组里一行裸文字 `详情 →`，被外层 `@click.stop` 吞掉点击。
    // 定位用**绑定**而不是文案：模板上方的注释里也提到「详情 →」，按文案找会命中注释。
    // 取**最后一次**：第一次是整卡的 `@click`（那正是能正常工作的那条路径），
    // 我们要守的是按钮组里那个单独的「详情」按钮。
    const binding = PAGE.lastIndexOf('@click="openDetail(row)"')
    expect(binding).toBeGreaterThan(0)

    const element = PAGE.slice(binding - 160, binding + 120)
    expect(element).toContain('<el-button')
    expect(element).toContain('详情 →')
  })

  it('「一键重建」调的是逐篇投递，而不是整库构建', () => {
    // 按钮本身只**开确认框**（它会覆盖整库已有索引），真正的循环在确认之后。
    const binding = PAGE.indexOf('@click="askReindexAll(row)"')
    expect(binding).toBeGreaterThan(0)

    const element = PAGE.slice(binding - 500, binding + 40)
    expect(element).toContain('一键重建')
    // 逐篇投递的实现必须在页面里（调的是单篇重建接口，不是 start_build）
    expect(PAGE).toContain('reindexDocumentConsole(')
    // 反例：换成「起一次整库构建」。那会走 start_build → 新版本 → 收敛 → 激活，
    // 与用户要的「把这些篇重新排进队列」是两件事。
    expect(element).not.toContain('startBuild')
  })

  it('构建进行中禁用「一键重建」（构建正在重切这个库，两批写会互相打）', () => {
    const binding = PAGE.indexOf('@click="askReindexAll(row)"')
    const element = PAGE.slice(binding - 500, binding + 40)

    expect(element).toContain(':disabled=')
    expect(element).toContain('bulkReindexing')
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
