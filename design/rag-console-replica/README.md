# RAG-Core 前端复刻（液态玻璃版 · 静态设计稿）

仓库 `frontend/`（Vue 3 + Element Plus + Tailwind）的视觉复刻：品牌色与结构沿用仓库现有令牌，
表层改成**液态玻璃**——半透明白层、折射模糊、亮边高光、分层投影；背景是**一整块平的中性灰**，
不用任何渐变。

## 屏与路由

| 文件 | 对应路由 / 组件 | 顶栏角色 |
| --- | --- | --- |
| `login.html` | `views/LoginPage.vue`（`/login`，无顶栏） | — |
| `knowledge-bases.html` | `views/KnowledgeManagePage.vue`（`/knowledge-bases`） | 租户 |
| `kb-detail.html` | 同上 + 库内文档详情（手机上是底部 sheet） | 租户 |
| `keys.html` | `views/KeysPage.vue`（`/keys`） | 租户 |
| `retrieve.html` | `views/RetrievePage.vue`（`/retrieve`）+ 运行摘要 / 六段耗时 / 对比面板 | 租户 |
| `traces.html` | `views/TracesPage.vue`（`/traces`） | 租户 |
| `admin-accounts.html` | `views/admin/AccountsPage.vue` | 管理员 |
| `admin-plans.html` | `views/admin/PlansPage.vue` | 管理员 |
| `admin-audit.html` | `views/admin/AuditPage.vue` | 管理员 |
| `admin-models.html` | `views/admin/ModelsPage.vue` | 管理员 |
| `admin-retrieval.html` | `views/admin/RetrievalPage.vue` | 管理员 |
| `admin-observability.html` | `views/admin/ObservabilityPage.vue` | 管理员 |
| `usage.html` | 新增：租户用量（`/usage`） | 租户 |
| `usage-key.html` | 新增：单 Key 用量（`/usage` 点进来） | 租户 |
| `usage-untracked.html` | 新增：用量页的「未统计」状态（`RATE_LIMIT_ENABLED=false`，额度计数未写入） | 租户 |
| `admin-usage.html` | 新增：站点用量（`/admin/usage`） | 管理员 |
| `admin-tenant.html` | 新增：管理员下钻到某个租户 | 管理员 |
| `admin-evaluation.html` | `views/EvaluationPage.vue`（`/admin/evaluation`） | 管理员 |
| `not-found.html` | `views/NotFoundPage.vue`（兜底路由） | 租户 |
| `kb-empty.html` | `KnowledgeManagePage.vue` 空态（`tree.length === 0`） | 租户 |
| `login-error.html` | `LoginPage.vue` 失败态（20010 / 会话过期） | — |
| `admin-error.html` | `AccountsPage.vue` 读取失败态（20000）+ 骨架 | 管理员 |

导航 5 条（租户）/ 8 条（管理员）与 `utils/access.ts` 一致，屏与屏之间是真链接。

## 用量四屏（`usage*.html` / `admin-usage.html` / `admin-tenant.html`）

唯一的「有 JS」的部分：图表用 ECharts（`assets/echarts.min.js`），共用层在 `usage-charts.js`，
局部样式在 `usage.css`。三条约定：

- **配色不在这里写死**：`usage-charts.js` 在挂载时读 `--ink` / `--muted` / `--line-lighter` /
  `--accent` 等令牌，主题那枚三段开关一变就重读重画——和图外那层玻璃是同一套颜色。
- **时间窗可点**：1 小时 / 1 天 / 7 天 / 30 天，点一下 KPI、图、表格一起重算。口径（含「今天未完整」
  「今日额度按 UTC 日切」「长窗 P95 来自直方图桶」）写在 `usage-decisions.md`，页面上不复述。
- **图表容器固定高度**（`.chart-box` 三档），不给卡片留「加载中忽然长高」的空间。

`admin-observability.html` 同时改了头部：多一枚「全部 / 只看成功 / 只看失败」三段开关，指标卡
从「检索次数」改成「请求数（成功 + 失败）」，降级率在默认的「全部」下显示「—」——被挡下的请求
既不降级也不空结果。

## 设计变量（`tokens.css`）

`:root` **最前面**有一组「风格导出的语义名」，别删：`--bg` / `--text` / `--primary` / `--brand` /
`--surface` / `--border` / `--radius-card` / `--radius-control` / `--shadow-card-flat`。

设计台「存成我的风格」不读页面，只按**变量名**认角色（底色要叫 bg/background/canvas、
正文要叫 text/ink/fg、主色要叫 primary/brand，还要 surface / muted / line / radius / shadow / font），
认不出 `primary` 就直接判「这套设计没用设计变量」并禁用按钮。内部一直用 `--accent`，没有 `primary`，
所以补了别名；而且**必须放在最前**——它遇到第一个名字匹配的变量就停下，放后面会先命中
`--glass-fill-card`（半透明白）当 surface、`--stroke-hi`（白）当 line，存出来的风格就是错的。

- **底**：`--canvas #e4e8ef`，纯色平铺，没有渐变、没有彩色光晕。
- **玻璃**：`--glass-grad` / `--glass-grad-card` / `--glass-grad-strong` 三档厚度（薄→厚），
  配 `--glass-blur blur(28px) saturate(185%)`、`--glass-stroke` 亮边、`--glass-highlight` 内高光、
  `--glass-shadow` / `--glass-shadow-lift` 两档投影。
- **文字**：`--ink #1d1d1f`、`--muted #5b5b61`（玻璃上要更沉才够 4.5:1）。
- **强调**：`--accent #0071e3`（沿用仓库的品牌蓝），按钮是蓝色玻璃药丸。
- 圆角 `24px` 卡片 / `14px` 控件 / `980px` 药丸；字号阶梯 11–44。

## 文案纪律

这一版把**解释性文字**全部砍掉：

- 页面副标题、卡片头/卡脚的说明句、一句话式提示（「原句直查，零模型调用」「可热改：…」）、
  字段旁的补充解释、重复的状态标签（角色名、「详情」、「详情 →」）都删了；
- 检索参数页直接删掉「为什么在这一档」整列，表格回到「配置项 / 有效值 / 来源」三列；
- 删掉之后含义靠**布局**补：档位用分组与徽章、来源用点状标记、异常用淡红底 + 角标、
  指标卡用「名称 + 大数字 + 单位」三段式、当前页在移动端排到导航轨道最前；
- 只保留：字段名、表头、状态、数值、控件文字、计数、空态/错误态里最短的一句。

## 主题三态（跟随 / 浅 / 深）

每屏顶栏右侧（账号胶囊左边）有一个三段小开关：◐ 跟随系统 · ☀ 浅色 · ☾ 深色。
登录与登录失败页没有顶栏，同一个控件以 `.theme-switch--float` 浮在右下角。

**不用一行 JS**：三个 `input[type=radio]` 存状态，`tokens.css` 用 `:has()` 读：

```css
@media (prefers-color-scheme: dark) { :root { …深色… } }          /* 跟随 */
html:has(#theme-dark:checked) { …深色… }                          /* 手动深 */
@media (prefers-color-scheme: dark) {
  html:has(#theme-light:checked) { …浅色… }                        /* 手动浅，覆盖系统深色 */
}
```

深色那套：底 #14161a、玻璃改成「极低 alpha 的白叠半透黑」、亮边收窄到 16%、投影加深、
文字与语义色换一档（accent 用 #0a84ff）。**页面与组件不写任何明暗分支**——所有填充/描边
都走 `--fill-hi/md/lo/xlo`、`--stroke-hi/md/lo`、`--sheen`、`--well-shadow`；
`prefers-reduced-transparency` / `prefers-contrast` 的实底引用 `--glass-solid-*`。

⚠️ 三份变量必须同步改（跟随 / 手动深 / 手动浅）。浅色那份是从 `:root` 默认值复制的，
改主题时三处一起改，否则「选浅」会在系统深色下留下几项深色残留。

- 窄屏（≤900px）隐藏顶栏那三段：手机上跟系统就够，顶栏要留宽度给导航轨道；浮动版不受影响。
- 键盘可达：radio 视觉隐藏但仍可 Tab，焦点环画在 label 上（`.theme-input:focus-visible + .theme-opt`）。

## 材质化与滚动边缘

- 玻璃不是「淡入」：页面顶层的面播一次 `glass-in`（`blur(6px)→0` + `scale(.988)→1`），
  每块晚 45ms，220ms、`cubic-bezier(.2,.8,.2,1)`；`prefers-reduced-motion` 下全局关掉。
- 顶栏下沿**不加任何色带**：栏高等于胶囊高（`padding-bottom: 0`），内容滚到胶囊下沿即被盖住，
  由胶囊自身的模糊 + 更实的填充（`--glass-grad` 顶栏专用，浅色 0.92/0.72、深色 0.96/0.92）负责压掉；
  投影另用更轻的 `--glass-shadow-nav`。之前两版都试过在栏底压渐变遮罩带，结果分别是
  「内容露半截再被切断」和「深色下一整块厚黑」，所以彻底去掉。
- `.card--raise` 是第四档厚度：一屏只用在一块，「当前正在看/正在跑」的那个对象。

## 移动端（按 mobile-native 清单）

- `viewport-fit=cover` + `interactive-widget=resizes-content`，`--canvas` 作 `theme-color`；
- 顶栏在 ≤900px 变成两行：第一行品牌 + 账号/登出，第二行是**可横滑的导航轨道**
  （`overflow-x:auto`、两端渐隐 mask、隐藏滚动条、`scroll-snap`，当前项 `order:-1` 排最前）——
  原来它会折成四行、吃掉 178px（屏高 844 的 1/5）；
- 弹窗在 ≤640px 变成**底部 sheet**（上沿两个圆角、抓手条、让出 `env(safe-area-inset-bottom)`）；
- `100dvh` / `100svh` 替代 `100vh`；`env(safe-area-inset-*)` 给顶栏与页面左右留白；
- `-webkit-tap-highlight-color: transparent`、`touch-action: manipulation`、
  控件 `user-select:none`、`overscroll-behavior: none`（内层滚动容器 `contain`）；
- 触摸设备输入框 16px（避免 iOS 聚焦缩放），可点目标 ≥ 40–44px；
- **所有 `:hover` 都包在 `@media (hover: hover) and (pointer: fine)` 里**，
  触摸时不会「点一下就一直亮着」；按压反馈走 `:active`（`scale(.97)`，120–160ms）。
- 三档无障碍降级：`prefers-reduced-transparency` / `prefers-contrast` / `prefers-reduced-motion`
  会把玻璃换成实底、加深描边、关掉动效。

## 排版细节

- **数字列右对齐**：纯数值列（耗时 / 库数 / 评分 / 样本数 / P50 / P95 / 通过率）挂 `.num-end`
（`text-align:right` + `tabular-nums`，并用 `.table .num-end` 压过 `.table th` 的左对齐）；
混排列（「214 块」、时间、run_id）保持左对齐。单位走 `.unit`（12px、`--muted`、与数字隔 3px），
百分号走 `.unit--pct`（贴紧：`86%`）。
- **焦点环**：`outline 2px accent` + 外侧 1px 白 + 5px 蓝晕，浅色玻璃与深色玻璃上都切得出来。
- **`.card--raise`**：第四档厚度，一屏只用在一处（例：评测页「当前这一轮」）。

## 与原站的已知差异

1. 数据是 mock，但字段口径照源码，同屏数字互相自洽。
2. 没有交互：弹窗、折叠、下拉按默认态渲染；知识库详情单独做了一屏。
3. `retrieve.html` 的 `h1` 照源码是「调试台」，顶栏入口才叫「检索调试台」。
   诊断区（运行摘要 / 六段耗时）默认收起，标题行右侧留一行等宽摘要（`hybrid · top_k 5 · pro`、`412 ms`），
   首屏不做长滚动；展开后是完整读数。
4. 窄屏表格改为横向滚动（`overscroll-behavior-x: contain`），不折字。

## 怎么看

右侧设计工作台已打开这套设计，同目录 21 个 HTML 并排显示；也可以直接浏览器打开 `login.html` 开始点。
