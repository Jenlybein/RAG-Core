import { describe, expect, it } from 'vitest'

/**
 * 本票的验收核心是一条**架构边界**而不是某个函数的行为：
 *
 * > 数据面客户端只剩一个调用点：`ragService.retrieve()`（`POST /api/v1/rag/retrieve`）。
 *
 * 这条边界靠人肉 grep 守住的话，下一票只要在某个页面里 `import dataHttp` 就悄悄破了，
 * 而且破的时候测试全绿。所以把它写成一个可执行的断言：扫 `src/` 里所有 `.ts` / `.vue`
 * 的源码文本，只允许 `services/ragService.ts` 从 `lib/dataHttp` 取客户端。
 *
 * 用 Vite 的 `import.meta.glob(..., { query: '?raw' })` 读源码而不是 `node:fs`：
 * 这个前端工程没有装 `@types/node`，为一条边界断言加一份类型依赖不值得；`?raw` 恰好
 * 能在不改变运行时的情况下拿到全部源文件。`__tests__` 目录被排除——测试里 mock
 * 这个模块本身是合法的。
 */
const SOURCES = import.meta.glob('../../**/*.{ts,vue}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

/** 一条真正取客户端或实例的 import 语句；注释里提到 `lib/dataHttp` 不算。 */
const DATA_HTTP_IMPORT = /(?:from\s*|import\()\s*['"](?:[^'"]*\/)?(?:lib\/)?dataHttp['"]/m

function importersOfDataHttp(): string[] {
  return Object.entries(SOURCES)
    // glob 的键是相对**本测试文件**的路径：同目录文件（本目录的其它测试）是 `./x`，
    // 其余测试文件带 `__tests__` 段；两者都不是「页面或服务 import 了这个客户端」。
    .filter(([path]) => !path.startsWith('./') && !path.includes('__tests__'))
    .filter(([, source]) => DATA_HTTP_IMPORT.test(source))
    .map(([path]) => path.replace(/^\.\.\/\.\.\//, ''))
    .sort()
}

describe('数据面调用点边界', () => {
  it('全仓库只有 services/ragService.ts 一个文件 import dataHttp', () => {
    // 期望值是写死的单元素列表：新增一个调用点的意思是「两张身份证混了」，
    // 而不是「多了一个便利函数」。改这里之前先回到 issue #92。
    expect(importersOfDataHttp()).toEqual(['services/ragService.ts'])
  })
})
