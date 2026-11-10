import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // API_KEY 这个变量名不是惯例的 VITE_ 前缀；不显式放行，浏览器端就永远读不到它，
  // 数据面请求会变成「没带 Key」——那会让 20010 看起来像后端坏了。
  envPrefix: ['VITE_', 'API_'],
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
