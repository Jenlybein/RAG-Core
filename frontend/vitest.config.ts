import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  // 组件测要能 import `.vue`（`RunSummary` / `ComparePanel`）；纯函数测不受影响。
  plugins: [vue()],
  test: {
    // 默认仍是 node：现有纯函数（信封解包、写头判定、diff 计算）都不需要 DOM。
    // 需要 DOM 的组件测在文件头用 `// @vitest-environment jsdom` 单独声明，
    // 不动这里的全局环境——那会连带改变现有那些用例的运行时。
    environment: 'node',
    // 约定里单测文件名是 `*.spec.ts`；T1 已有的两个文件用了 `*.test.ts`，
    // 两种都收，避免为了改名去动别人的测试。
    include: ['src/**/*.test.ts', 'src/**/*.spec.ts'],
  },
})
