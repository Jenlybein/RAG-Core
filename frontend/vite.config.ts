import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // 只放行 VITE_ 前缀的变量：数据面的 Key 不再来自构建期（#92 删掉了 API_KEY 兜底），
  // 身份只来自页面里选中的那一把，经 `lib/activeKey.ts` 只进内存。
  envPrefix: 'VITE_',
  server: {
    // 固定 loopback + 端口：控制面 cookie 的 Path=/console 是给同源开发代理用的，
    // 换个 host 或端口就可能被浏览器当成另一个站点，登录态与探针结果都会对不上。
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // 数据面与控制面在浏览器眼里都是同源路径：后者靠 cookie，跨源就得动 SameSite。
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/console': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
