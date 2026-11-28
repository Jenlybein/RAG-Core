/**
 * 知识库页的**模板结构守卫**（第 17 步 #226 / #227 的回归）。
 *
 * ## 为什么是结构断言而不是挂载测试
 *
 * 这里守的几处都是「模板里少一个东西、页面就静默失效」的形态，而它们的失效**不报错**：
 *
 * 1. 卡片底部的「详情 →」曾经是一行**裸文字**，而它所在的按钮组带 `@click.stop`（防误触发
 *    整卡点击）。于是点它既不发请求、也不冒泡到卡片的 `@click` —— 用户看到的是「点了没反应」，
 *    控制台没有任何报错，请求也没有。
 * 2. 「重建」曾经调的是逐篇重投（`reindexAllDocuments`），用户点完发现「需重建」提示还挂着
 *    —— 逐篇写回的是**当前生效版本**，参数快照不变，提示当然消不掉。判据是**调哪个接口**。
 * 3. 详情弹窗曾经没有自己的请求（读列表上一次轮询的快照），而列表在「全终态」时会静默停掉
 *    轮询——于是「进详情要按 F5 才看到真状态」。判据是「详情里有自己的查询与一句更新时刻」。
 *
 * 这些缺陷用挂载测试能抓，但整页挂载要拖进 vue-query、router、Element Plus 与 API 层四套
 * 替身；而真正的判据只有积木级的几句话，扫源码比挂载更直接，也更快。反过来说：这个文件
 * **不能**替代对接口行为的测试——那在 `tests/test_console_kb_read_model.py`（后端口径）与
 * `lib/__tests__/refreshPolicy.spec.ts`（刷新策略）里。
 */

import { describe, expect, it } from 'vitest'

// 用 Vite 的 `?raw` 读源码：`node:fs` / `__dirname` 在容器里的 `vue-tsc` 下没有类型
// （镜像里不做全量 dev 依赖安装），而 `?raw` 是打包器自己的能力，两边行为一致。
import PAGE from '../KnowledgeManagePage.vue?raw'
import CHART from '../../components/UsageChart.vue?raw'
import LIVE_STATUS from '../../components/LiveStatus.vue?raw'
import QUERIES from '../../composables/useKbQueries.ts?raw'
import CONSOLE_KB from '../../services/consoleKb.ts?raw'

describe('知识库卡片的操作区', () => {
  it('整卡可点 = 详情入口，且按钮组里不再有那个多余的「详情」', () => {
    const card = PAGE.indexOf('role="button"')
    expect(card).toBeGreaterThan(0)
    expect(PAGE.slice(card, card + 900)).toContain('@click="openDetail(row)"')

    // 反例（曾经的样子）：按钮组里又放了一个「详情 →」。它要么是**裸文字**（被外层
    // `@click.stop` 吞掉点击，点了没反应），要么与整卡点击完全重复——用户的原话是
    // 「一点用都没有这个按钮」。所以这里守的是「别再加回来」。
    const groupStart = PAGE.indexOf('pt-2.5')
    const groupEnd = PAGE.indexOf('</el-card>', groupStart)
    expect(groupStart).toBeGreaterThan(0)
    expect(groupEnd).toBeGreaterThan(groupStart)
    expect(PAGE.slice(groupStart, groupEnd)).not.toContain('详情')
  })

  it('「重建」调的是整库重建，不是逐篇重新投递', () => {
    const binding = PAGE.indexOf('@click="askRebuild(row)"')
    expect(binding).toBeGreaterThan(0)

    const element = PAGE.slice(binding - 700, binding + 40)
    expect(element).toContain('重建')
    // 判据是**调哪个接口**：只有产出新版本才消得掉「需重建」提示；逐篇重建写回旧版本，
    // 文档都重切了一遍、提示还挂着（第一版就是这个毛病）。
    expect(PAGE).toContain('rebuildKbConsole(')
    expect(PAGE).not.toContain('reindexAllDocuments(')
  })

  it('构建进行中禁用「重建」（并发构建由后端守，这里别给注定失败的按钮）', () => {
    const binding = PAGE.indexOf('@click="askRebuild(row)"')
    const element = PAGE.slice(binding - 700, binding + 40)

    expect(element).toContain(':disabled=')
    expect(element).toContain('rebuildingKbId')
    // 判据跟着契约走：`build` 摘要带 `status`（第 17 步 #227 之后不再有 build_progress）。
    expect(element).toContain("row.build?.status === 'building'")
  })

  it('卡片上的四个数字读**聚合字段**，不再按文档数组求和', () => {
    // 摘要不再带 documents 数组（一个 5000 篇的库不该为了四个数字读 5000 行）。
    // 这条会抓住「有人偷偷把文档数组加回来」与「模板回退成 documents.length」。
    // 锚在**模板里那一格**（脚本里 `dialogMessage` 也出现过「篇文档」，锚错地方会拿到脚本段）。
    const metrics = PAGE.indexOf('metric-label">篇文档')
    expect(metrics).toBeGreaterThan(0)
    const block = PAGE.slice(metrics - 400, metrics + 800)
    expect(block).toContain('row.document_count')
    expect(block).toContain('row.chunk_count')
    expect(block).not.toContain('documents.length')
    expect(block).not.toContain('chunkTotal(')
  })

  it('有失败文档时卡片上直接说数量（不用点进详情去数）', () => {
    expect(PAGE).toContain('row.status_counts.failed')
  })
})

describe('详情弹窗：自己的请求、自己的节奏', () => {
  const dialogStart = PAGE.indexOf('v-model="detailOpen"')
  const dialog = PAGE.slice(dialogStart, PAGE.indexOf('原文：只读', dialogStart))

  it('弹窗用的是自己的查询（不是列表那一份）', () => {
    expect(dialogStart).toBeGreaterThan(0)
    expect(QUERIES).toContain('useKbDocumentsQuery')
    expect(QUERIES).toContain('fetchKbDocuments')
    // 反例：详情读列表的树（那是「进详情要按 F5」的根）。
    expect(PAGE).not.toContain('useTreePolling')
    expect(PAGE).not.toContain('treeQuery')
  })

  it('弹窗里有「更新于几点」+ 手动刷新', () => {
    // 一段会停的刷新必须在界面上自证它停在哪一刻；这一条比把间隔调多小重要得多。
    expect(dialog).toContain('<LiveStatus')
    expect(dialog).toContain(':updated-at="detailUpdatedAt"')
    expect(dialog).toContain(':mode="detailMode"')
    expect(LIVE_STATUS).toContain('更新于')
    expect(LIVE_STATUS).toContain("emit('refresh')")
  })

  it('弹窗里有构建进度（它以前只在卡片上，一开弹窗就被盖住了）', () => {
    expect(dialog).toContain('detailBuild')
    expect(dialog).toContain('重建中')
    expect(dialog).toContain('progressPercent(detailBuild)')
  })

  it('文档表有「本次构建」第二根轴，且不改文档自己的状态列', () => {
    // 构建期间文档状态几乎一直是「成功」（每篇只有自己那一秒是「索引中」），
    // 所以「还在重建」只能靠第二列说出来。合并成一个状态会污染文档的状态机。
    expect(dialog).toContain('本次构建')
    expect(dialog).toContain("row.build_state === 'done'")
    expect(dialog).toContain("row.build_state === 'pending'")
    expect(dialog).toContain("row.build_state === 'failed'")
    expect(dialog).toContain('<StatusBadge')
  })

  it('文档表分页（库可能几千篇，一次全取会把弹窗做成几 MB）', () => {
    expect(dialog).toContain('<el-pagination')
    expect(dialog).toContain(':total="detailTotal"')
    expect(QUERIES).toContain('pageSize')
  })

  it('关键字交给服务端筛（有防抖），不是前端过滤一页', () => {
    expect(dialog).toContain('服务端筛选')
    // 防抖本身是**纯函数**（`createDebouncer`），在 `composables/__tests__/kbQueries.spec.ts`
    // 里用假定时器钉住；这里只守「它确实被接进这条查询」。
    expect(QUERIES).toContain('createDebouncer')
    expect(QUERIES).toContain('KEYWORD_DEBOUNCE_MS')
    // 反例：详情里自己 filter 一遍（那只会在**当前页**里找，用户以为「没有这篇」）。
    expect(dialog).not.toContain('.filter((doc) =>')
  })

  it('没生效版本时说实话（「未建索引」而不是「0 个分块」）', () => {
    // 「还没建过索引」要去做一件事（重建），「索引里有 0 片」是另一件事（去查）。
    // 一个 0 会让人以为后者，而它其实连版本都还没有。
    expect(PAGE).toContain('has_active_version')
    expect(PAGE).toContain('未建')
  })

  it('关窗即停：清掉 kb_id，别让关掉的弹窗继续轮询', () => {
    // `enabled` 只看 kbId：只把 detailOpen 置 false，那条查询还在按 3 秒/60 秒打后端。
    expect(PAGE).toContain('@closed="closeDetail"')
    expect(PAGE).toContain('detailKbId.value = null')
  })

  it('「已生效」要配一个可核对的时刻', () => {
    expect(PAGE).toContain('activated_at')
    expect(PAGE).toContain('clockOf(')
  })
})

describe('列表页：摘要单独一条请求', () => {
  it('列表用 fetchKbList（摘要，不带文档数组）', () => {
    expect(QUERIES).toContain('fetchKbList')
    expect(QUERIES).toContain('KB_LIST_QUERY_KEY')
    expect(CONSOLE_KB).toContain('KB_LIST_PATH')
    // 反例：列表又回去读树（一次把整个租户的文档读出来）。
    expect(QUERIES).not.toContain('fetchTree')
  })

  it('横幅按**真实理由**写话（recent 不许念 running 那句台词）', () => {
    // 用户的原话：「明明都写着已生效了，顶上还说有构建在跑」。模式只有两档，文案不能写死
    // 一句——否则「刚收尾、再跟几轮」这一档就会说错话。
    expect(PAGE).toContain("listReason === 'running'")
    expect(PAGE).toContain('刚有构建收尾')
    expect(PAGE).toContain('有构建在跑或有文档在排队/索引中')
    expect(PAGE).toContain('listRefreshReason')
  })

  it('列表页也有「更新于几点」+ 两档刷新', () => {
    expect(PAGE).toContain(':updated-at="listUpdatedAt"')
    expect(PAGE).toContain(':mode="listMode"')
    expect(QUERIES).toContain('listRefreshMode')
  })

  it('写操作之后失效**整组**读模型（列表 / 详情 / 构建一起）', () => {
    // 少失效一个键，症状就是「改完这一屏那一屏还是旧的」。
    expect(PAGE).toContain('invalidateQueries({ queryKey: KB_QUERY_PREFIX })')
    expect(CONSOLE_KB).toContain("KB_QUERY_PREFIX = ['knowledge-bases']")
    expect(CONSOLE_KB).toContain("KB_LIST_QUERY_KEY = ['knowledge-bases', 'list']")
    expect(CONSOLE_KB).toContain("KB_DOCUMENTS_QUERY_KEY = ['knowledge-bases', 'documents']")
  })
})

describe('用量曲线图', () => {
  it('第四本账（切块）在图上，且走**右轴**——它与前三本量纲不同', () => {
    const chart = CHART

    expect(chart).toContain("name: '切块（token）'")
    expect(chart).toContain('yAxisIndex: 1')
    // 反例：把切块堆进 `stack: 'meters'` 那根柱子——柱高会变成「次数 + token」。
    const at = chart.indexOf("name: '切块（token）'")
    const chunkSeries = chart.slice(at, at + 400)
    expect(chunkSeries).not.toContain("stack: 'meters'")
  })
})

describe('卡片上的「N 篇待更新」（第 17 步现场九·第三步）', () => {
  it('那句话挂在整条构建状态链**之外**，不会顶掉「上次构建未完成」', () => {
    // 曾经的样子：`<p v-if="stale_documents > 0">` 插在 `v-else-if(active)` 与
    // `v-else-if(failed)` 之间。Vue 的 v-if / v-else-if 是**链式**的，插进中间会把
    // 后面那条挂到新条件上——于是构建失败、又没有待更新篇的库**看不到失败提示**，
    // 而那正是最该被看见的一个。这里守的就是位置：它必须排在 failed 分支之后。
    const failed = PAGE.indexOf('上次构建未完成')
    const stale = PAGE.indexOf('data-testid="kb-stale-documents"')
    expect(failed).toBeGreaterThan(0)
    expect(stale).toBeGreaterThan(failed)

    // 并且它自己必须是独立的 `v-if`（写成 v-else-if 就又回到链里了）。
    const tagStart = PAGE.lastIndexOf('<p', stale)
    expect(PAGE.slice(tagStart, stale)).toContain('v-if')
    expect(PAGE.slice(tagStart, stale)).not.toContain('v-else-if')
  })

  it('文案说「设置与产物不一致」而不是「设置改过」', () => {
    // 判据里也包含**从没记过指纹**的存量篇，那批不是「改过」。文案与判据对不上时，
    // 用户会去查「谁改了设置」，而那件事根本没发生。
    const stale = PAGE.indexOf('data-testid="kb-stale-documents"')
    expect(PAGE.slice(stale, stale + 300)).toContain('设置与产物不一致')
    expect(PAGE.slice(stale, stale + 300)).not.toContain('设置改过')
  })
})
