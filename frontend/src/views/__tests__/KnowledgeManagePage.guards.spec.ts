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
 * 2. 「重建」曾经调的是逐篇重投（`reindexAllDocuments`），用户点完发现「待更新」提示还挂着。
 *    判据是**调哪个接口**：全库重切是 `rebuildKbConsole`，逐篇是 `reindexDocumentConsole`。
 * 3. 详情弹窗曾经没有自己的请求（读列表上一次轮询的快照），而列表在「全终态」时会静默停掉
 *    轮询——于是「进详情要按 F5 才看到真状态」。判据是「详情里有自己的查询与一句更新时刻」。
 * 4. 「N 篇待更新」那句按篇读数曾经被插进 v-if / v-else-if 链中间，把后面那条「失败」提示
 *    挂到新条件上——链里少一个分支比多一个分支危险得多，因为它**只在某种数据下**消失。
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
import SETTINGS_DIALOG from '../../components/KbSettingsDialog.vue?raw'
import QUERIES from '../../composables/useKbQueries.ts?raw'
import REFRESH_POLICY from '../../lib/refreshPolicy.ts?raw'
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

  it('「重建」调的是整库重切入口，不是逐篇重新投递', () => {
    const binding = PAGE.indexOf('@click="askRebuild(row)"')
    expect(binding).toBeGreaterThan(0)

    const element = PAGE.slice(binding - 700, binding + 40)
    expect(element).toContain('重建')
    // 判据是**调哪个接口**：整库重切走 `/rebuild`（后端按篇投递整个库），逐篇重建走
    // `documents/{id}/reindex`。一个几百篇的库不该让用户点几百次。
    expect(PAGE).toContain('rebuildKbConsole(')
    expect(PAGE).not.toContain('reindexAllDocuments(')
  })

  it('空库与正在请求重建时禁用「重建」（别给注定失败 / 会连点的按钮）', () => {
    const binding = PAGE.indexOf('@click="askRebuild(row)"')
    const element = PAGE.slice(binding - 700, binding + 40)

    expect(element).toContain(':disabled=')
    expect(element).toContain('rebuildingKbId')
    // 空库没有可投递的文档（后端会拒），正被点的那个库也不该允许再点一次。
    expect(element).toContain('row.document_count === 0')
    // 构建层已经退场：禁用条件里不能再出现 `build.status` 这种参照物。
    expect(element).not.toContain('build?.status')
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

  it('文档表只有文档自己的一根轴：状态徽章 + 分块数，没有「本次构建」列', () => {
    // 构建层退场后参照物没了：每一篇自己就有当前代，状态四态就是它的进度。
    // 这里守的是「别把那一列加回来」（那意味着又引入了一份库级构建账本）。
    expect(dialog).toContain('<StatusBadge')
    expect(dialog).toContain('row.status')
    expect(dialog).toContain('row.chunk_count')
    expect(dialog).not.toContain('本次构建')
    expect(dialog).not.toContain('build_state')
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

  it('详情 header 直接说切片数，不再有「未建索引」这个库级版本状态', () => {
    // 「这个库有没有生效版本」随版本层一起退场：切片数 0 就是现在检索得到 0 片，
    // 不再有「还没建过」这种要说两句话的状态。
    expect(dialog).toContain('detailKb.chunk_count')
    expect(PAGE).not.toContain('has_active_version')
    expect(PAGE).not.toContain('未建索引')
  })

  it('关窗即停：清掉 kb_id，别让关掉的弹窗继续轮询', () => {
    // `enabled` 只看 kbId：只把 detailOpen 置 false，那条查询还在按 3 秒/60 秒打后端。
    expect(PAGE).toContain('@closed="closeDetail"')
    expect(PAGE).toContain('detailKbId.value = null')
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
    // 一句——否则「刚写完、再跟几轮」这一档就会说错话。判据只剩两条数据：文档状态与最近写入。
    expect(PAGE).toContain("listReason === 'running'")
    expect(PAGE).toContain('有文档在排队或索引中')
    expect(PAGE).toContain('刚有写入')
    expect(PAGE).toContain('listRefreshReason')
    expect(PAGE).not.toContain('有构建在跑')
  })

  it('列表页也有「更新于几点」+ 两档刷新', () => {
    expect(PAGE).toContain(':updated-at="listUpdatedAt"')
    expect(PAGE).toContain(':mode="listMode"')
    expect(QUERIES).toContain('listRefreshMode')
  })

  it('刷新判据里有两个时间信号：文档状态与最近写入（都不是构建行）', () => {
    // 构建层退场后「有在途工作」只能从数据里读：四态计数 + 写操作时刻/文档落库时刻。
    expect(REFRESH_POLICY).toContain('hasPendingDocuments')
    expect(REFRESH_POLICY).toContain('recentlyWritten')
    expect(REFRESH_POLICY).toContain('latestWriteAt')
    // 本页写操作的打点必须在刷新**之前**：顺序反了会先排一轮 60 秒慢查。
    // （判的是 `afterWrite` 函数体里的顺序，不是文件里的距离——`refresh` 定义在它前面。）
    const afterWrite = PAGE.indexOf('async function afterWrite')
    expect(afterWrite).toBeGreaterThan(0)
    const body = PAGE.slice(afterWrite, afterWrite + 220)
    const stamp = body.indexOf('lastWriteAt.value = Date.now()')
    const refreshCall = body.indexOf('await refresh()')
    expect(stamp).toBeGreaterThan(0)
    expect(refreshCall).toBeGreaterThan(stamp)
  })

  it('写操作之后失效**整组**读模型（列表与详情一起）', () => {
    // 少失效一个键，症状就是「改完这一屏那一屏还是旧的」。
    expect(PAGE).toContain('invalidateQueries({ queryKey: KB_QUERY_PREFIX })')
    expect(CONSOLE_KB).toContain("KB_QUERY_PREFIX = ['knowledge-bases']")
    expect(CONSOLE_KB).toContain("KB_LIST_QUERY_KEY = ['knowledge-bases', 'list']")
    expect(CONSOLE_KB).toContain("KB_DOCUMENTS_QUERY_KEY = ['knowledge-bases', 'documents']")
  })
})

describe('构建层已经退场（旧版不再兼容）', () => {
  it('页面与设置弹窗里不再有任何构建 / 生效版本 / 本次构建的残留', () => {
    // 第 17 步·最后一批：整库「构建版本」层退场，索引产物下放到篇级。这些字段与提示
    // 在旧契约里存在过，留一句在模板上就等于把一个已经不存在的概念摆给用户看。
    for (const legacy of [
      'build_state',
      'build_progress',
      'needs_rebuild',
      'has_active_version',
      'activated_at',
      '上次构建未完成',
      '重建中',
      '本次构建',
      '已生效',
    ]) {
      expect(PAGE).not.toContain(legacy)
      expect(SETTINGS_DIALOG).not.toContain(legacy)
    }
    // 契约层（服务与映射）也不能把它加回来。
    expect(CONSOLE_KB).not.toContain('build_progress')
    expect(CONSOLE_KB).not.toContain('needs_rebuild')
    expect(REFRESH_POLICY).not.toContain('buildInFlight')
    expect(REFRESH_POLICY).not.toContain('buildJustFinished')
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
  it('那句话挂在整条提示链**之外**，不会顶掉「N 篇索引失败」', () => {
    // 曾经的样子：`<p v-if="stale_documents > 0">` 插在 `v-else-if(active)` 与
    // `v-else-if(failed)` 之间。Vue 的 v-if / v-else-if 是**链式**的，插进中间会把
    // 后面那条挂到新条件上——于是失败、又没有待更新篇的库**看不到失败提示**，
    // 而那正是最该被看见的一个。这里守的就是位置：它必须排在失败提示之后。
    const failed = PAGE.indexOf('篇索引失败')
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
