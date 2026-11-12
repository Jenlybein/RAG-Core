import { describe, expect, it } from 'vitest'

/**
 * 本票的验收核心是一条**架构边界**而不是某个函数的行为：
 *
 * > 数据面客户端的调用点**只能是被明确列举的那几个**，而且每一个都是「拿真实 Key
 * > 打真实请求」的业务动作，不是页面里顺手发的请求。
 *
 * 当前三个（都在 `services/` 下，逐个写死在期望值里）：
 * - `ragService.ts` → `POST /api/v1/rag/retrieve`（检索）；
 * - `feedbackService.ts` → `POST /api/v1/rag/feedback`（#106 起，给这次检索打分）。
 *   反馈的归属校验用的是这把 Key 解出的租户，所以它**必须**与检索同一把身份；
 *   这不是「多了一个便利函数」，而是数据面契约的一部分。
 * - `scopeService.ts` → `GET /api/v1/knowledge-bases`（**只读发现**：这把 Key 能查哪些库）。
 *   加它的理由：这个问题只有拿这把 Key 问服务端才作数——早先是前端拿控制面的
 *   `allowed_kb_ids` 自己算的，那是客户端算的答案，与服务端判定漂了就会显示一个
 *   服务端并不认可的集合，而错误要等检索撞 20008 才出现。
 *
 * 这条边界靠人肉 grep 守住的话，下一票只要在某个页面里 `import dataHttp` 就悄悄破了，
 * 而且破的时候测试全绿。所以把它写成一个可执行的断言：扫 `src/` 里所有 `.ts` / `.vue`
 * 的源码文本，只允许这几处从 `lib/dataHttp` 取客户端。
 *
 * **两条加粗的底线**（就是为了防止「顺手再加一个」把边界磨没）：
 * 1. 页面（`.vue`）一个都不许直接 import —— 身份只从 service 层进来；
 * 2. 这些文件里**不许出现写入类端点**（上传 / 删除 / 重建 / 建库）——数据面调用只能做
 *    检索、反馈与只读发现，写操作一律走控制面会话门。
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

/** 数据面**写入类**端点：出现即说明有人在拿 Bearer 身份做写操作。 */
const WRITE_ENDPOINTS = ['/documents/upload', '/documents/', '/knowledge-bases/create']

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
  it('只有被列举的 services 文件 import dataHttp', () => {
    // 期望值写死且**逐个列举**：新增一个调用点的意思是「两张身份证混了」，
    // 而不是「多了一个便利函数」。改这里之前先回到 issue #92 与 #106。
    expect(importersOfDataHttp()).toEqual([
      'services/feedbackService.ts',
      'services/ragService.ts',
      'services/scopeService.ts',
    ])
  })

  it('页面组件一个都不许直接 import dataHttp', () => {
    // 身份只从 service 层进来：页面里出现 dataHttp，就意味着「这一次用哪个身份」
    // 多了一个可能的答案。
    const pages = importersOfDataHttp().filter((path) => path.endsWith('.vue'))

    expect(pages).toEqual([])
  })

  it('这些文件里不出现数据面写入类端点', () => {
    const offenders = importersOfDataHttp().filter((path) => {
      const relative = `../../${path}`
      const source = SOURCES[relative] ?? ''
      return WRITE_ENDPOINTS.some((endpoint) => source.includes(endpoint))
    })

    expect(offenders).toEqual([])
  })
})
