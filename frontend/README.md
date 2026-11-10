# RAG-Core 调试台前端

Vue 3 + TypeScript + Vite，包管理器只用 pnpm（与后端的 uv 一样，不混 npm/yarn）。

```bash
pnpm install
pnpm dev   # http://127.0.0.1:5173 ，/probe 是临时探针页（T6 后删除）
pnpm test  # vitest run
```

为什么要两个客户端：数据面的身份是 API Key（`Authorization: Bearer`），控制面的身份是会话 cookie（写请求还要带 `x-requested-with`）。两套身份若共用一套拦截器，迟早会出现「拿明文 Key 打控制面」或「把 20011 当成掉登录」的事故，所以 `src/lib/dataHttp.ts` 与 `src/lib/consoleHttp.ts` 是两个独立实例，只共用信封剥壳与错误文案。

接口事实以 `rag-notes/step8/00-facts.md` 为准；开发代理同时覆盖 `/api` 与 `/console`，浏览器不接触跨域。
