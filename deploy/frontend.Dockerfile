# RAG-Core 前端：构建产物由 nginx 静态服务，并把 /api 与 /console 反代到 API 容器。
#
#   docker compose -f deploy/docker-compose.yml up -d --build frontend
#
# **为什么要一个 nginx 而不是 vite preview**：控制面会话 cookie 的 Path 是 `/console`，
# 浏览器把「同源」当登录态的一部分。前端与 API 必须同源——由 nginx 在同一个 origin 下
# 把两类路径转给 API，浏览器就只看到一个站点，cookie 与防跨站头都不用改。
#
# **为什么构建期不设 VITE_API_BASE_URL**：`lib/dataHttp.ts` / `lib/consoleHttp.ts` 在它为空时
# 走相对路径，正好被 nginx 接住。填成绝对地址会绕开代理，等于把「同源」这件事丢了。

# ── 构建层 ────────────────────────────────────────────────────────────────────
FROM node:22-bookworm-slim AS build

# npm 源：默认走 npmmirror。官方源在部分网络下只有几十 KiB/s，装一次依赖要十几分钟，
# 而构建期是最不该赌网络的地方。能直连官方源的机器用 --build-arg 覆盖回去即可：
#   docker compose -f deploy/docker-compose.yml build frontend \
#     --build-arg NPM_REGISTRY=https://registry.npmjs.org
ARG NPM_REGISTRY=https://registry.npmmirror.com
# 同时喂给 corepack（它自己要从 registry 下 pnpm 本体）与 pnpm 装包。
ENV npm_config_registry=${NPM_REGISTRY}

# pnpm 版本与仓库锁文件同源（frontend/pnpm-lock.yaml，lockfileVersion 9.0）。corepack 随 node 自带。
ENV PNPM_HOME=/pnpm \
    PATH="/pnpm:$PATH"
RUN corepack enable

WORKDIR /app

# 依赖层：只有 package.json / 锁文件 / pnpm 设置变了才重装。
#
# `pnpm-workspace.yaml` **必须一起拷**：构建脚本白名单（`allowBuilds: vue-demi`）只在那里，
# pnpm 12 不再读 `package.json` 的 `pnpm` 字段。缺了它 `pnpm install --frozen-lockfile`
# 会以 `ERR_PNPM_IGNORED_BUILDS` 退出 1——vue-demi 的 postinstall 要按实际 Vue 版本切换入口，
# 不跑它 @tanstack/vue-query 会拿到错的那份。
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# 源码层：先拷配置再拷源码，改一个 .vue 不必重装依赖。
COPY frontend/ ./
# `pnpm build` = `vue-tsc --noEmit && vite build`：类型不过就构建失败。
# 这里刻意**不**跳过类型检查——镜像里跑起来的必须是能过类型的那一版。
RUN pnpm build

# ── 运行层 ────────────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine

# SPA 路由回退 + 两条反代规则都在这个文件里，与 vite.config.ts 的开发代理逐条对应。
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# nginx 镜像自带的 entrypoint 会读 /etc/nginx/conf.d/*.conf；不需要额外 CMD。
