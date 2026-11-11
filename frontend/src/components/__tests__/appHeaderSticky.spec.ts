import { describe, expect, it } from 'vitest'

/**
 * 顶栏的定位契约：**吸顶、实心、不靠模糊**。
 *
 * 这三条都不是审美偏好，是被用户当成 bug 报过一遍的现场：
 *
 * 1. 没有 `sticky top-0` 时，顶栏会跟着页面一起滚走——导航和「当前是谁」直接消失，
 *    这不是风格问题，是可用性问题。
 * 2. 用 `bg-canvas/85 + backdrop-blur` 做半透明时，内容会从顶栏底下透出来；滚动中看到
 *    的是一片半透明的移动色块，读起来像「顶栏自己在动」。
 * 3. `backdrop-filter` 会把顶栏提升成独立合成层，贴着 sticky 用时 Chrome 上有重绘抖动。
 *
 * 用源码扫描而不是渲染测试：`AppHeader` 依赖 vue-query、router 与 ElMessage，为一条
 * class 断言把整套上下文搭起来不值得；而这条契约恰好是「模板里写了什么」，扫文本足够，
 * 且和 `dataPlaneBoundary.spec.ts` 是同一套做法。
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

describe('顶栏定位契约', () => {
  it('顶栏吸顶（sticky top-0，且必须有 z-index 压住内容）', () => {
    const tag = headerOpenTag()
    expect(tag).toContain('sticky')
    expect(tag).toContain('top-0')
    // 没有 z-index 的 sticky 会被后面出现的内容盖住（main 也是定位元素）
    expect(tag).toMatch(/\bz-\d+/)
  })

  it('顶栏背景不透明：不出现半透明底色与 backdrop-blur', () => {
    const tag = headerOpenTag()
    // `bg-canvas/85` 这种带斜杠的透明度一旦回归，内容就会从顶栏底下透出来
    expect(tag).toMatch(/\bbg-[a-z-]+(?!\/)/)
    expect(tag).not.toMatch(/bg-[a-z-]+\/\d+/)
    expect(tag).not.toContain('backdrop-blur')
  })
})
