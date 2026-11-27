<script setup lang="ts">
/**
 * `/keys`：本租户的 Key 列表 + 签发 + 获取 Key（重新生成）+ 编辑密钥配置 + 删除。
 *
 * 页面自己只做三件事：什么时候请求（`useQuery` / 直接 await 的写操作）、弹哪个框、
 * 把结果放到哪。请求形状归 `services/keyService.ts`，判定（哪些字段算改动、预设怎么填、
 * 过渡期还剩多久）归 `utils/keyPolicy.ts` / `utils/keyRotation.ts`——组件里不再写第二份。
 *
 * 三条与安全相关的取舍（issue #92，ADR-0004 修订版）：
 *
 * - **列表里只有打码形态**。库里只存 scrypt 哈希与末 5 位，原文只在**签发 / 重新生成
 *   那一刻**的响应里出现过一次（`key_plaintext`），所以页面没有「显示原文」这个动作可做；
 *   能做的是在弹窗里当场把那一串联交给人（复制 / 直接去调试台试一把），关掉就不再保留；
 * - **重新生成 ≠ 签发**：前者给同一把 Key 换凭证，旧明文进入 24 小时过渡期；后者是新增
 *   一把，不影响任何旧 Key。两条路径的警示文案因此不能共用——签发路径里提「过渡 Key」
 *   会让用户以为签发会动到别的 Key；
 * - 管理员的 `tenant_id` 为空，用户面板接口对他一律 20011；路由守卫（#85）已经把他挡在
 *   `/admin/*`，本页不重写这道判定。
 */
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ConfirmDialog from '../components/ConfirmDialog.vue'
import KeyPolicyEditor from '../components/KeyPolicyEditor.vue'
import OneTimeKeyDialog from '../components/OneTimeKeyDialog.vue'
import PageHeader from '../components/PageHeader.vue'
import { writeRetrieveApiKey } from '../lib/apiKeySession'
import { ApiError } from '../lib/envelope'
import { CONSOLE_ME_QUERY_KEY, fetchMe } from '../services/console'
import {
  KEYS_QUERY_KEY,
  createKey,
  deleteKey,
  fetchKeys,
  patchPolicy,
  regenerateKey,
  revokePreviousKey,
} from '../services/keyService'
import { KNOWLEDGE_BASE_TREE_QUERY_KEY, fetchTree } from '../services/knowledgeBaseService'
import { useRetrieveStore } from '../stores/retrieve'
import {
  MAX_UNIT_CHARS_DEFAULT,
  TOP_K_DEFAULT,
  type IssuedKeyData,
  type KbOption,
  type KeyData,
  type KeyPolicyData,
  type PolicyForm,
} from '../types/key'
import { buildPolicyPatch, policyFormFrom, toPolicyInput } from '../utils/keyPolicy'
import { isTransitionActive, prevKeyTooltip } from '../utils/keyRotation'
import { keyStatusLabel, statusPillStyle } from '../utils/statusPill'

const route = useRoute()
const router = useRouter()
const queryClient = useQueryClient()
const retrieveStore = useRetrieveStore()

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const base = error.code === -1 ? error.message : `${error.message}（业务码 ${error.code}）`
    // 20015 的通用文案（「参数非法或配置互相矛盾」）说不出到底是哪一项；配置自洽校验的
    // 细节只在后端原文里，排版上附在后面，用户才知道该改哪个开关。
    if (error.code === 20015 && error.backendMsg !== '') return `${base}：${error.backendMsg}`
    return base
  }
  return error instanceof Error ? error.message : String(error)
}

// —— 读：我的套餐 / 控制面树 / Key 列表 ——

const meQuery = useQuery({
  queryKey: CONSOLE_ME_QUERY_KEY,
  queryFn: fetchMe,
  retry: false,
})
const plan = computed(() => meQuery.data.value?.plan ?? null)

const treeQuery = useQuery({
  // 与列表页 / 检索页共用同一份树缓存：库清单本来就是同一个接口。
  queryKey: KNOWLEDGE_BASE_TREE_QUERY_KEY,
  queryFn: fetchTree,
  retry: false,
})
const kbOptions = computed<KbOption[]>(() =>
  (treeQuery.data.value ?? []).map((node) => ({ kb_id: node.kb_id, name: node.name })),
)

const keysQuery = useQuery({
  queryKey: KEYS_QUERY_KEY,
  queryFn: fetchKeys,
  retry: false,
})
const keys = computed<KeyData[]>(() => keysQuery.data.value ?? [])
const keysError = computed(() =>
  keysQuery.error.value === null ? '' : errorText(keysQuery.error.value),
)
// 模板只对顶层 ref 自动解包；`keysQuery.isPending` 是嵌套的 Ref，先摘出来再用。
const keysPending = keysQuery.isPending

// —— 表格摘要 ——

/**
 * el-table 的插槽只把行标成 `DefaultRow`（不是我们的 `KeyData`），所以在这里做一次
 * 形状判定：模板里调用这几个辅助函数时，边界处的类型断言只写这一处。
 */
function isKeyRow(row: unknown): row is KeyData {
  return (
    typeof row === 'object' &&
    row !== null &&
    'key_id' in row &&
    'key_masked' in row &&
    'status' in row
  )
}

/**
 * 配置摘要拆成若干「标签块」（chips），而不是一整串文字。
 *
 * 一整句话放在表格列里，列一窄就折行，而且会折在词中间——看着像坏了。
 * 拆成 chips 之后，换行只发生在块与块之间，每一块都是完整语义，怎么折都不难看。
 */
function policyChips(row: unknown): string[] {
  const key = row as KeyData
  const policy: KeyPolicyData | null = key.policy
  if (policy === null) return ['配置缺失']
  const capabilities: string[] = []
  if (policy.retrieve_allowed) capabilities.push('retrieve')
  if (policy.rewrite_allowed) capabilities.push('rewrite')
  if (policy.decompose_allowed) capabilities.push('decompose')
  if (policy.research_allowed) capabilities.push('research')
  if (policy.rerank_allowed) capabilities.push('rerank')
  return [
    `${policy.retrieval_mode} · top_k ${policy.retrieval_top_k}${policy.retrieval_rerank ? ' · 重排' : ''}`,
    capabilities.length === 0 ? '无授权能力' : capabilities.join(' / '),
    `白名单 ${policy.allowed_kb_ids.length} 个库`,
    `${policy.retrieve_qps} 次/秒`,
    `${policy.retrieve_daily} 次/天`,
  ]
}

function keyLabel(row: unknown): string {
  const key = row as KeyData
  return key.label === null || key.label.trim() === '' ? '（未命名）' : key.label
}

/**
 * 这一行此刻还有过渡窗口吗。
 *
 * 判据是 `prev_key_expires_at > now`，**不是** `prev_key_masked !== null`：提前作废实现成
 * 「把截止时刻改到过去」，旧址的打码形态会保留（还要继续显示末 5 位、继续给审计用）。
 * 只看打码形态会让图标与「作废旧 Key」在窗口结束后继续挂着，点下去只剩一句
 * 「已经没有可作废的过渡 Key」。
 *
 * `now` 在渲染时取：tooltip 的内容必须跟着「此刻」走，而这一行的截止时刻是后端在轮换
 * 那一刻写死的。
 */
function transitionActive(row: KeyData): boolean {
  return isTransitionActive(row.prev_key_expires_at, Date.now())
}

/**
 * 状态徽标旁 `ⓘ` 的 tooltip。
 *
 * 只在窗口内调用（模板里有同一个 `transitionActive` 守着，图标与文案因此不会各自出现）。
 */
function prevKeyTooltipOf(row: KeyData): string {
  if (row.prev_key_masked === null) return ''
  return prevKeyTooltip(row.prev_key_masked, row.prev_key_expires_at, Date.now())
}

// —— 签发 / 编辑对话框 ——

/** `null` 表示「签发一把新的」，否则是在编辑这一把 Key 的配置。 */
const editingKey = ref<KeyData | null>(null)
const editorOpen = ref(false)
const issueLabel = ref('')
const saving = ref(false)
const form = ref<PolicyForm>(newPolicyForm())

/**
 * 新建一套配置的初始值。
 *
 * 与后端 `KeyPolicyInput` 的默认值同义：只开基线 retrieve 一个位、加工能力的两本账
 * 全 0（新 Key 不该自带一次改写或一次 Research）、形状全开、白名单空（什么都没圈定）。
 * 检索限额默认落在套餐上限内——「忘了填」不该被一个看起来合理的硬编码掩盖。
 */
function newPolicyForm(): PolicyForm {
  const planValue = plan.value
  return {
    allowed_kb_ids: [],
    retrieve_allowed: true,
    rewrite_allowed: false,
    decompose_allowed: false,
    research_allowed: false,
    rerank_allowed: false,
    retrieve_qps: planValue?.retrieve_qps ?? 1,
    retrieve_daily: planValue?.retrieve_daily ?? 1,
    rerank_qps: 0,
    rerank_daily: 0,
    llm_qps: 0,
    llm_daily: 0,
    retrieval_mode: 'vector',
    retrieval_top_k: TOP_K_DEFAULT,
    retrieval_rerank: false,
    include_parent: true,
    expand_table_rows: true,
    max_unit_chars: MAX_UNIT_CHARS_DEFAULT,
  }
}

function openIssue(): void {
  editingKey.value = null
  issueLabel.value = ''
  form.value = newPolicyForm()
  editorOpen.value = true
}

function openEdit(row: unknown): void {
  if (!isKeyRow(row)) return
  const key = row
  if (key.policy === null) {
    // 配置独立于 Key，可能已被删掉或指向别的租户（坏数据）。这时不编一套默认配置出来。
    ElMessage.warning('这把 Key 没有可编辑的密钥配置（配置可能已被删除），请重新签发一把')
    return
  }
  editingKey.value = key
  issueLabel.value = key.label ?? ''
  form.value = policyFormFrom(key.policy)
  editorOpen.value = true
}

const editorTitle = computed(() =>
  editingKey.value === null ? '签发 API Key' : '编辑密钥配置',
)

function closeEditor(): void {
  editorOpen.value = false
  editingKey.value = null
}

const canSave = computed(() => {
  if (saving.value) return false
  if (form.value.retrieve_qps < 1 || form.value.retrieve_daily < 1) return false
  if (form.value.retrieval_top_k < 1) return false
  return true
})

async function save(): Promise<void> {
  if (!canSave.value) return
  const target = editingKey.value
  if (target === null) {
    // 签发是不可撤销的一步，而且明文只显示一次：先把这句话摆在用户面前，确认之后才真正
    // 发请求。警示放在请求之后，就等于把承诺写成事后通知。
    //
    // 为什么挂在「签发」这个提交动作上、而不是页面顶部的「签发新 Key」按钮上：那个按钮
    // 先打开的是**配置表单**，请求根本还没发。若在按钮上弹警示，用户确认完还要再填一遍
    // 表单，那份「我知道」就成了一次路标而不是承诺；而这里弹出的正是真正的最后一步，
    // 确认后立刻走请求、成功后同一弹窗切到展示态。表单内容此时已经就绪，不会丢。
    openKeyDialog('issue', null)
    return
  }
  saving.value = true
  try {
    if (target.policy === null) return
    const patch = buildPolicyPatch(form.value, target.policy)
    if (Object.keys(patch).length === 0) {
      ElMessage.info('没有改动，未提交')
      return
    }
    const result = await patchPolicy(target.policy.policy_id, patch)
    await queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY })
    ElMessage.success(
      result.changed.length > 0 ? `已保存，变更字段：${result.changed.join('、')}` : '已保存',
    )
    closeEditor()
  } catch (error) {
    // 失败时保留对话框：用户多半只想改一个值再试。错误文案走 **toast** 而不是框内红字——
    // 20015 的正文是「哪一列超了哪个上限」，框内那一行在长表单底部很容易被忽略；
    // 停留久一点、可手动关掉，够读完再改。
    ElMessage.error({
      message: errorText(error),
      duration: 8000,
      showClose: true,
    })
  } finally {
    saving.value = false
  }
}

// —— 「获取 Key」：警示态 →（确认请求）→ 展示态 ——

type KeyDialogMode = 'regenerate' | 'issue'

const keyDialogOpen = ref(false)
const keyDialogMode = ref<KeyDialogMode>('regenerate')
/** 重新生成的目标行；签发路径为 `null`（新 Key 的 id 要等响应回来才知道）。 */
const keyDialogTarget = ref<KeyData | null>(null)
/**
 * 响应里那串**唯一**的明文。
 *
 * 它是原文在本页唯一的副本：库里没有、列表接口也不会再给。关掉弹窗即置空——不写
 * storage、不进 URL、不留在别的 ref；想再用只能重新生成或重签一把。
 */
const keyDialogResult = ref<IssuedKeyData | null>(null)
const keyDialogLoading = ref(false)

function openKeyDialog(mode: KeyDialogMode, target: KeyData | null): void {
  keyDialogMode.value = mode
  keyDialogTarget.value = target
  keyDialogResult.value = null
  keyDialogOpen.value = true
}

/** 行内的「获取 Key」：重新生成当前这把（key_id / 配置 / 白名单都不变）。 */
function openGetKey(row: unknown): void {
  if (!isKeyRow(row)) return
  openKeyDialog('regenerate', row)
}

function closeKeyDialog(): void {
  keyDialogOpen.value = false
  keyDialogTarget.value = null
  keyDialogResult.value = null
}

/**
 * 警示态点了确认：这时才发请求。
 *
 * 两种模式共用同一个弹窗与同一段「关掉就看不到」的流程；区别只在调哪个接口、以及成功后
 * 列表里该出现什么（签发多一行，重新生成是同一行换了凭证并出现过渡 Key）。
 */
async function confirmKeyDialog(): Promise<void> {
  if (keyDialogLoading.value) return
  keyDialogLoading.value = true
  try {
    if (keyDialogMode.value === 'issue') {
      const created = await createKey({
        label: issueLabel.value,
        policy: toPolicyInput(form.value),
      })
      // 失效后**等**刷新完成再切展示态：列表先跟上，用户关掉弹窗时看到的已经是新状态。
      await queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY })
      closeEditor()
      keyDialogResult.value = created
      return
    }

    const target = keyDialogTarget.value
    if (target === null) return
    const regenerated = await regenerateKey(target.key_id)
    await queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY })
    keyDialogResult.value = regenerated
  } catch (error) {
    // 失败时留在警示态：请求没成，没有任何不可挽回的事发生，用户关掉重试即可。
    ElMessage.error({
      message: errorText(error),
      duration: 8000,
      showClose: true,
    })
  } finally {
    keyDialogLoading.value = false
  }
}

// —— 提前作废过渡 Key ——

const pendingRevoke = ref<KeyData | null>(null)
const revokeOpen = ref(false)
const revoking = ref(false)

const revokeMessage = computed(() => {
  const key = pendingRevoke.value
  if (key === null || key.prev_key_masked === null) return ''
  // 后果说在前面：仍在使用旧 Key 的业务会立即失败，而且作废不能撤销——这句话必须在点
  // 「作废」之前被读到，而不是在失败之后才出现在错误提示里。
  return `确认让过渡 Key ${key.prev_key_masked} 立刻失效？仍在使用旧 Key 的业务会立即失败，且无法撤销。`
})

function askRevoke(row: unknown): void {
  if (!isKeyRow(row) || row.prev_key_masked === null) return
  // 入口只在窗口内出现（模板里有 `transitionActive` 守着）；这里再挡一次，是为了不依赖
  // 「模板的条件恰好和这里一致」。窗口已关时作废没有任何可作废的东西。
  if (!transitionActive(row)) return
  pendingRevoke.value = row
  revokeOpen.value = true
}

async function confirmRevoke(): Promise<void> {
  const key = pendingRevoke.value
  if (key === null) return
  revoking.value = true
  try {
    const receipt = await revokePreviousKey(key.key_id)
    await queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY })
    // `revoked=false` 是**幂等答案**而不是错误，且有两种成因：本来就没有过渡密钥，或者
    // 窗口已经到期 / 已经被作废过。两种对用户都是同一件事——没有可作废的东西了，所以
    // 一句 info 就够，不该红字，也不该让人以为操作失败。
    if (receipt.revoked) ElMessage.success('旧 Key 已作废')
    else ElMessage.info('这把 Key 已经没有可作废的过渡 Key')
    revokeOpen.value = false
    pendingRevoke.value = null
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    revoking.value = false
  }
}

/**
 * 「去调试台试用」：把这串交给检索页的请求层，然后跳过去。
 *
 * 除了内存里的 `lib/activeKey`，还要写进调试台读取的 `sessionStorage` 键：刷新调试台
 * 之后它得还在——否则「签发完顺手去试」这一步会在 F5 之后变回「请粘贴 Key」，
 * 而用户刚才明明拿到的就是这一串。
 */
function goToDebug(): void {
  const result = keyDialogResult.value
  if (result === null) return
  retrieveStore.selectKey({
    keyId: result.key_id,
    keyValue: result.key_plaintext,
    allowedKbIds: result.policy?.allowed_kb_ids ?? [],
  })
  writeRetrieveApiKey(result.key_plaintext)
  closeKeyDialog()
  void router.push('/retrieve')
}

// —— 删除 ——

const pendingDelete = ref<KeyData | null>(null)
const deleteOpen = ref(false)
const deleting = ref(false)

const deleteMessage = computed(() => {
  const key = pendingDelete.value
  if (key === null) return ''
  // 删的是凭证不是配置：说清楚这一区别，否则用户会担心「配置也没了」。
  return `确认删除《${keyLabel(key)}》（${key.key_masked}）？删掉后无法恢复，它的密钥配置会保留。`
})

function askDelete(row: unknown): void {
  if (!isKeyRow(row)) return
  pendingDelete.value = row
  deleteOpen.value = true
}

async function confirmDelete(): Promise<void> {
  const key = pendingDelete.value
  if (key === null) return
  deleting.value = true
  try {
    await deleteKey(key.key_id)
    // 如果检索页正选着这把，清掉选择：留着一把已删除的 Key 只会让下一次检索拿到 20010，
    // 而那个码看起来像「Key 失效」，用户不会想到是自己刚删了它。
    if (retrieveStore.selectedKeyId === key.key_id) retrieveStore.clearKey()
    await queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY })
    ElMessage.success('已删除这把 Key，配置已保留')
    deleteOpen.value = false
    pendingDelete.value = null
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    deleting.value = false
  }
}

// —— 「去改这把 Key 的配置」深链：`/keys?key_id=...` 直接打开它的编辑器 ——

/** 只自动打开一次：打开后清掉 query，避免刷新 / 返回时再次弹出同一个框。 */
let openedFromQuery = false

watch(
  [keys, () => route.query.key_id],
  () => {
    if (openedFromQuery) return
    const raw = route.query.key_id
    const keyId = Array.isArray(raw) ? raw[0] : raw
    if (typeof keyId !== 'string') return
    const key = keys.value.find((item) => item.key_id === keyId)
    if (key === undefined) return
    openedFromQuery = true
    openEdit(key)
    void router.replace({ path: '/keys' })
  },
  { immediate: true },
)
</script>

<template>
  <section class="space-y-6">
  <PageHeader
    title="API Key"
  />
    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <span class="th-label">共 {{ keys.length }} 把</span>
          <el-button type="primary" @click="openIssue">签发新 Key</el-button>
        </div>
      </template>

      <p v-if="keysPending" class="text-sm text-slate-400">正在读取 Key 列表…</p>
      <div
        v-else-if="keysError !== ''"
        class="rounded-control border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
      >
        {{ keysError }}
      </div>

      <div v-else-if="keys.length === 0" class="py-8 text-center">
        <p class="text-[15px] font-semibold text-ink">还没有 Key</p>
        <el-button type="primary" class="!mt-5" @click="openIssue">签发新 Key</el-button>
      </div>

      <!--
        列表做成卡片行而不是表格：原来「配置摘要」一列塞着一整句话，列一窄就折行、
        还断在词中间。改成卡片后每一行的信息按「名字 → 配置块 → 操作」排下来，
        宽度不够时只有 chips 会在块之间换行，永远是完整的语义块。

        masked Key 不再单独占一行：它只是「这是哪一把」的辅助信息，而短 id 已经承担了
        同一件事。把「看原文」留在右侧操作里，左列就只剩真正要扫读的两行。
      -->
      <ul v-else class="divide-y divide-line">
        <li
          v-for="row in keys"
          :key="row.key_id"
          class="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 py-4 first:pt-1 last:pb-1"
        >
          <div class="min-w-0 flex-1 space-y-2">
            <!-- 第一层：名字 + 状态。
                 key_id 不显示：它是数据面的行身份证，对使用者没有意义（要操作这把 Key
                 就点右边按钮），而露出 UUID 只会让人以为那是一串「密钥」，
                 反而增加「能不能贴到别处用」的误判。 -->
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-sm font-semibold text-ink">{{ keyLabel(row) }}</span>
              <span
                class="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium"
                :style="statusPillStyle(row.status)"
              >
                {{ keyStatusLabel(row.status) }}
              </span>
              <!--
                过渡 Key 的提示只在**过渡窗口还没结束时**出现。判据是 `prev_key_expires_at > now`，
                不是 `prev_key_masked !== null`：作废实现成「把窗口截止时刻改到过去」，旧址的
                打码形态会保留（显示末 5 位、给审计用）。窗口关掉之后这里什么都不剩——不写
                「旧 Key 已失效」这类追认文案：用户要的是「现在还能不能过校验」，不是变更史。
              -->
              <el-tooltip
                v-if="transitionActive(row)"
                :content="prevKeyTooltipOf(row)"
                placement="top"
                :show-after="200"
              >
                <span
                  class="cursor-help text-xs text-muted"
                  aria-label="过渡 Key 的可用情况"
                  data-testid="prev-key-hint"
                >ⓘ</span>
              </el-tooltip>
            </div>

            <!-- 第二层：配置摘要拆成 chips，换行只在块之间发生 -->
            <div class="flex flex-wrap items-center gap-1.5">
              <span v-for="chip in policyChips(row)" :key="chip" class="chip">
                {{ chip }}
              </span>
            </div>
          </div>

          <div class="flex shrink-0 flex-wrap items-center gap-1">
            <!--
              「获取 Key」只在行内出现：空状态页继续用「签发新 Key」，那里没有「已有一把
              可以重新生成」可言。这两个动作用的是同一段警示 → 展示流程（同一个组件）。
            -->
            <el-button link type="primary" size="small" @click="openGetKey(row)">获取 Key</el-button>
            <!--
              作废入口与过渡期提示同进同退：窗口结束后链接不出现，用户不会看到一个
              点下去只会回「已经没有可作废的过渡 Key」的按钮。
            -->
            <el-button
              v-if="transitionActive(row)"
              link
              type="primary"
              size="small"
              @click="askRevoke(row)"
            >
              作废旧 Key
            </el-button>
            <el-button
              link
              type="primary"
              size="small"
              :disabled="row.policy === null"
              @click="openEdit(row)"
            >
              编辑配置
            </el-button>
            <!--
              文案刻意是「删除 Key」而不是「删除配置」：这个按钮删的是凭证行，
              密钥配置（key_policy）会原样保留——确认框里也是这么写的。写成「删除配置」
              会让人以为按下去是把那套配置清掉，正好说反。
            -->
            <el-button link type="danger" size="small" @click="askDelete(row)">删除 Key</el-button>
          </div>
        </li>
      </ul>
    </el-card>

    <!-- 签发 / 编辑 -->
    <el-dialog
      v-model="editorOpen"
      append-to-body
      :title="editorTitle"
      width="880px"
      :close-on-click-modal="!saving"
      :close-on-press-escape="!saving"
      :show-close="!saving"
    >
      <el-form v-if="editingKey === null" label-position="top" class="!mb-1">
        <el-form-item label="备注名（可选）">
          <el-input
            v-model="issueLabel"
            maxlength="128"
            show-word-limit
            placeholder="例如：集成 A"
          />
        </el-form-item>
      </el-form>
      <p v-else class="mb-3 text-xs text-slate-500">
        正在编辑《{{ keyLabel(editingKey) }}》（<code class="font-mono">{{ editingKey.key_masked }}</code>）
      </p>

      <KeyPolicyEditor
        v-model="form"
        :kb-options="kbOptions"
        :plan="plan"
      />

      <template #footer>
        <div class="flex justify-end gap-2">
          <el-button :disabled="saving" @click="closeEditor">取消</el-button>
          <el-button type="primary" :disabled="!canSave" :loading="saving" @click="save">
            {{ editingKey === null ? '签发' : '保存配置' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

    <!-- 删除二次确认 -->
    <ConfirmDialog
      v-model="deleteOpen"
      title="删除 API Key"
      :message="deleteMessage"
      confirm-text="删除"
      danger
      :loading="deleting"
      @confirm="confirmDelete"
    />

    <!-- 作废过渡 Key 的轻确认：说清「在用旧 Key 的业务会立即失败、且无法撤销」 -->
    <ConfirmDialog
      v-model="revokeOpen"
      title="作废旧 Key"
      :message="revokeMessage"
      confirm-text="作废"
      danger
      :loading="revoking"
      @confirm="confirmRevoke"
    />

    <!--
      「获取 Key」的唯一出口：警示态 → 展示态。签发与重新生成共用这一个组件——
      两者的共同点（明文只显示这一次、关掉不再保存）必须由同一段流程来兑现。
    -->
    <OneTimeKeyDialog
      v-model="keyDialogOpen"
      :mode="keyDialogMode"
      :plaintext="keyDialogResult?.key_plaintext ?? null"
      :loading="keyDialogLoading"
      @cancel="closeKeyDialog"
      @confirm="confirmKeyDialog"
      @debug="goToDebug"
    />
  </section>
</template>
