# T1 前端真实安装版本（供 T15 回填文档）

> 临时笔记，T15 把版本表回填 `00-复现总指引/02-环境准备.md` §1.3 后可删除本文件。
> 安装时间：2026-11-10；宿主：Node 22.23.2 / pnpm 12.5.1（Windows）。

## 直接依赖（`pnpm list --depth 0` 的实际解析结果）

| 包 | 版本 | 类型 |
|---|---|---|
| vue | 3.5.45 | runtime |
| vue-router | 4.6.4 | runtime |
| pinia | 4.1.0 | runtime |
| @tanstack/vue-query | 5.107.1 | runtime |
| axios | 1.21.0 | runtime |
| zod | 4.6.7 | runtime |
| element-plus | 2.14.8 | runtime |
| @element-plus/icons-vue | 2.3.2 | runtime |
| vite | 8.3.6 | dev |
| @vitejs/plugin-vue | 6.0.10 | dev |
| typescript | 5.9.3 | dev |
| vue-tsc | 3.3.14 | dev |
| vitest | 5.1.0 | dev |
| tailwindcss | 3.4.19 | dev（**钉在 3.x**，v4 没有 `tailwindcss init -p`） |
| postcss | 8.5.32 | dev |
| autoprefixer | 10.6.2 | dev |

## 与「当天稳定版」的两处偏差

1. **TypeScript 从最新稳定版 7.0.2 退到 5.9.3**：`vue-tsc@3.3.14` 仍要读 `typescript/lib/tsc`，
   而 TS 7 的包不再导出这个子路径，`vue-tsc` 直接抛 `ERR_PACKAGE_PATH_NOT_EXPORTED`。
   `pnpm build` 要绿，只能留在 TS 5.x；等 vue-tsc 支持 TS 7 再升。
2. **vite 实际解析为 8.3.6**：查询 registry 时 `pnpm view vite version` 为 8.3.7，
   但 pnpm 12 的 lockfile / supply-chain 校验固定到了 8.3.6；以 `pnpm list` 为准。

## 备注

- pnpm 12 不再读 `package.json` 的 `pnpm` 字段，构建脚本白名单写在 `frontend/pnpm-workspace.yaml`
  的 `allowBuilds`（`vue-demi` 的 postinstall 必须放行，否则 `@tanstack/vue-query`
  会拿到错版本的 vue-demi 入口）。
- `pnpm build` 有「chunk > 500 kB」提示（Element Plus 全量引入），不是错误；
  后续页面多了再按路由拆包。
