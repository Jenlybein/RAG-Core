/// <reference types="vite/client" />

// 只声明这一个自定义变量；`.env` 里的 API_KEY 兜底已随 #92 删除——数据面的身份
// 只来自页面选中的 Key（`lib/activeKey.ts`），不再有构建期兜底。
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
