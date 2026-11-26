import { describe, expect, it } from 'vitest'

/**
 * 顶栏的定位契约：**吸顶、磨砂但必须够实、并且有一条硬边界**。
 *
 * 历史：这条测试原本守的是「顶栏必须完全不透明、不许 backdrop-blur」——那时半透明的
 * 做法被当成 bug 报过（滚动时内容从栏下透出来，像栏在动）。后来整站换成 apple.com 的
 * 设计语言，磨砂顶栏是那套观感的标志之一，于是**契约本身改了**，测试跟着改，而不是把
 * 测试删掉。
 *
 * 第三次改：顶栏从「整条玻璃带」改成「透明吸顶栏 + 一枚玻璃胶囊」（设计稿）。
 * 三条契约的**实质没变，只是承担它的元素换了**：
 *
 * 1. `sticky top-0` + 非零 z-index：没有它，顶栏会跟着页面滚走、或被后面的内容盖住。
 *    这一条仍在 `<header>` 上。
 * 2. **磨砂要有底**：允许 `backdrop-blur`，但必须叠在一层**足够不透明**的白底上
 *    （≥70%），否则又回到「内容在栏下流动」的老问题。这条现在由胶囊承担
 *    （`data-header-capsule`）——严格说它要盖住的比整条栏还多：胶囊外的画布留白
 *    也必须看得到内容，所以只有胶囊需要「实」。
 * 3. **必须有一条边界**：纯模糊会让栏与内容糊在一起。胶囊上是一条 1px 亮边
 *    （`border`），替代原来的 `border-b`。
 *
 * 用源码扫描而不是渲染测试：`AppHeader` 依赖 vue-query、router 与 ElMessage，为几条
 * class 断言把整套上下文搭起来不值得；而这条契约恰好是「模板里写了什么」，扫文本足够，
 * 且与 `dataPlaneBoundary.spec.ts` 是同一套做法。
 */
const SOURCES = import.meta.glob('../../**/*.vue', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

function headerSource(): string {
  const entry = Object.entries(SOURCES).find(([path]) => path.endsWith('/AppHeader.vue'))
  if (entry === undefined) throw new Error('AppHeader.vue 不在 glob 结果里')
  return entry[1]
}

/** 取出 `<header ...>` 的起始标签，避免把整份文件里的注释一起匹配进来。 */
function headerOpenTag(): string {
  const match = headerSource().match(/<header\b[^>]*>/s)
  if (match === null) throw new Error('AppHeader.vue 里没有 <header> 起始标签')
  return match[0]
}

/**
 * 取出胶囊那一块的属性串。用 `data-header-capsule` 定位而不是 class 名：
 * 胶囊的 class 会随视觉调整变化，标记属性不会。
 */
function capsuleAttributes(): string {
  const match = headerSource().match(/<div\b[^>]*data-header-capsule[^>]*>/s)
  if (match === null) throw new Error('AppHeader.vue 里没有 data-header-capsule 标记')
  return match[0]
}

describe('顶栏定位契约', () => {
  it('顶栏吸顶（sticky top-0，且必须有 z-index 压住内容）', () => {
    const tag = headerOpenTag()
    expect(tag).toContain('sticky')
    expect(tag).toContain('top-0')
    // 没有 z-index 的 sticky 会被后面出现的内容盖住（main 也是定位元素）
    expect(tag).toMatch(/\bz-\d+/)
  })

  it('磨砂可以，但承担它的胶囊必须有够实的底（≥70%）', () => {
    const tag = capsuleAttributes()
    const translucent = tag.match(/bg-[a-z-]+\/(\d+)/)
    expect(translucent, '胶囊应当是「半透明底 + 模糊」的组合').not.toBeNull()

    // 关键：不许回到 10%~50% 那种「内容在胶囊下流动」的做法。
    expect(Number(translucent![1])).toBeGreaterThanOrEqual(70)
    expect(tag).toContain('backdrop-blur')
  })

  it('磨砂之外还要有一条边界（胶囊上的 1px 亮边）', () => {
    // 只有模糊时，页面上的栏与内容会糊在一起；这条边是「材料到哪儿结束」的唯一硬信号。
    expect(capsuleAttributes()).toMatch(/\bborder\b/)
  })
})
