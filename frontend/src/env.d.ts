/// <reference types="vite/client" />

// 给 import.meta.env 补上两个自定义变量；没有这行声明，TS 会把 API_KEY 当成拼写错误。
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL: string
  readonly API_KEY: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
