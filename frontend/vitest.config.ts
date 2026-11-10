import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // 本票只测纯函数（信封解包与写头判定），不需要 DOM；将来要挂组件再换 jsdom。
    environment: 'node',
    // 约定里单测文件名是 `*.spec.ts`；T1 已有的两个文件用了 `*.test.ts`，
    // 两种都收，避免为了改名去动别人的测试。
    include: ['src/**/*.test.ts', 'src/**/*.spec.ts'],
  },
})
