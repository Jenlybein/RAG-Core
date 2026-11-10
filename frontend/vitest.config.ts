import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // 本票只测纯函数（信封解包与写头判定），不需要 DOM；将来要挂组件再换 jsdom。
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
})
