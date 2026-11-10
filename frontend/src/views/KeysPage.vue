<script setup lang="ts">
/**
 * `/keys`：本租户的 Key 列表 + 签发 + 编辑密钥配置 + 删除。
 *
 * 页面自己只做三件事：什么时候请求（`useQuery` / 直接 await 的写操作）、弹哪个框、
 * 把结果放到哪。请求形状归 `services/keyService.ts`，判定（哪些档位被套餐挡住、
 * 哪些字段算改动、custom 参数何时生效）归 `utils/keyPolicy.ts`——组件里不再写第二份。
 *
 * 两条与安全相关的取舍（issue #92 / ADR-0004）：
 *
 * - **默认只显示 masked**。`GET /console/keys` 会回明文（调试台要拿原文打真实接口），
 *   但界面上先给 `rc_live_…9f2c`；要看原文必须二次确认，并且看完即弃——不写进任何
 *   storage、不进 URL、不留第二份 ref。列表数据里的明文是接口本来就会回的，页面无法也
 *   不该改变这一点，能做的是不主动展示、不持久化；
 * - 管理员的 `tenant_id` 为空，用户面板接口对他一律 20011；路由守卫（#85）已经把他挡在
 *   `/admin/*`，本页不重写这道判定。
 */
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage } from 'element-plus'
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import ConfirmDialog from '../components/ConfirmDialog.vue'
import KeyPolicyEditor from '../components/KeyPolicyEditor.vue'
import PageHeader from '../components/PageHeader.vue'
import { ApiError } from '../lib/envelope'
import { CONSOLE_ME_QUERY_KEY, fetchMe } from '../services/console'
import { KEYS_QUERY_KEY, createKey, deleteKey, fetchKeys, patchPolicy } from '../services/keyService'
import { KNOWLEDGE_BASE_TREE_QUERY_KEY, fetchTree } from '../services/knowledgeBaseService'
import { useRetrieveStore } from '../stores/retrieve'
import {
  CUSTOM_TOP_K_DEFAULT,
  type KbOption,
  type KeyData,
  type KeyPolicyData,
  type PolicyForm,
} from '../types/key'
import { buildPolicyPatch, policyFormFrom, toPolicyInput } from '../utils/keyPolicy'
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
const allowedProfiles = computed(() => plan.value?.allowed_profiles ?? [])
/**
 * 档位参数（含当前套餐没开放的那几档）。
 *
 * 后台改完档位参数、前台下一次进这个页面就能看到新值——两个进程读的是同一张表，
 * 而 `me` 的这份是注册表里的快照（管理端写入后立刻刷新）。
 */
const profilePresets = computed(() => meQuery.data.value?.profile_presets ?? [])

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
const keysLoaded = keysQuery.isSuccess

/** 编辑器里的套餐提示；`plan` 为空时给 `null`，组件就不摆那句「套餐允许」的空话。 */
const planHint = computed(() => {
  const value = plan.value
  if (value === null) return null
  return {
    researchAllowed: value.research_allowed,
    hybridAllowed: value.hybrid_allowed,
    rerankAllowed: value.rerank_allowed,
    queryRewriteAllowed: value.query_rewrite_allowed,
    retrieveQps: value.retrieve_qps,
    retrieveDaily: value.retrieve_daily,
  }
})

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
    'key_value' in row
  )
}

/**
 * 配置摘要拆成若干「标签块」（chips），而不是一整串文字。
 *
 * 原来那是一句话：`quality · hybrid / rerank / query_rewrite · 白名单 1 个库 · 10 次/秒 /
 * 5000 次/天`。它放在表格列里，列一窄就折行，而且会折在「hybrid /」中间——看着像坏了。
 * 拆成 chips 之后，换行只发生在块与块之间，每一块都是完整语义，怎么折都不难看。
 */
function policyChips(row: unknown): string[] {
  const key = row as KeyData
  const policy: KeyPolicyData | null = key.policy
  if (policy === null) return ['配置缺失']
  const capabilities: string[] = []
  if (policy.research_allowed) capabilities.push('research')
  if (policy.hybrid_allowed) capabilities.push('hybrid')
  if (policy.rerank_allowed) capabilities.push('rerank')
  if (policy.query_rewrite_allowed) capabilities.push('query_rewrite')
  return [
    policy.profile,
    capabilities.length === 0 ? '无额外能力' : capabilities.join(' / '),
    `白名单 ${policy.allowed_kb_ids.length} 个库`,
    `${policy.retrieve_qps} 次/秒`,
    `${policy.retrieve_daily} 次/天`,
  ]
}

function keyLabel(row: unknown): string {
  const key = row as KeyData
  return key.label === null || key.label.trim() === '' ? '（未命名）' : key.label
}

/** 只显示 id 的前 8 位：完整的 UUID 会把这一行撑开；要全的在 title 里悬停看。 */
function shortKeyId(row: unknown): string {
  const id = (row as KeyData).key_id
  return id.length <= 8 ? id : `${id.slice(0, 8)}…`
}

// —— 签发 / 编辑对话框 ——

/** `null` 表示「签发一把新的」，否则是在编辑这一把 Key 的配置。 */
const editingKey = ref<KeyData | null>(null)
const editorOpen = ref(false)
const issueLabel = ref('')
const saveError = ref('')
const saving = ref(false)
const form = ref<PolicyForm>(newPolicyForm())

/**
 * 新建一套配置的初始值。
 *
 * 三处刻意的默认：白名单空（什么都没圈定）、能力全关（最贵的两项不该新 Key 自带）、
 * 形状全开（与既有响应默认一致）。限额与档位必须有值，且默认落在套餐上限内——
 * 「忘了填」不该被一个看起来合理的硬编码掩盖。
 */
function newPolicyForm(): PolicyForm {
  const planValue = plan.value
  const defaultProfile = allowedProfiles.value[0] ?? 'speed'
  return {
    allowed_kb_ids: [],
    research_allowed: false,
    hybrid_allowed: false,
    rerank_allowed: false,
    query_rewrite_allowed: false,
    retrieve_qps: planValue?.retrieve_qps ?? 1,
    retrieve_daily: planValue?.retrieve_daily ?? 1,
    profile: defaultProfile,
    include_parent: true,
    expand_table_rows: true,
    custom_options: { mode: 'vector', top_k: CUSTOM_TOP_K_DEFAULT, rerank: false, rewrite: false },
  }
}

function openIssue(): void {
  editingKey.value = null
  issueLabel.value = ''
  saveError.value = ''
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
  saveError.value = ''
  form.value = policyFormFrom(key.policy)
  editorOpen.value = true
}

const editorTitle = computed(() =>
  editingKey.value === null ? '签发 API Key' : '编辑密钥配置',
)

function closeEditor(): void {
  editorOpen.value = false
  editingKey.value = null
  saveError.value = ''
}

const canSave = computed(() => {
  if (saving.value) return false
  if (form.value.profile === '') return false
  if (form.value.retrieve_qps < 1 || form.value.retrieve_daily < 1) return false
  return true
})

async function save(): Promise<void> {
  if (!canSave.value) return
  saveError.value = ''
  saving.value = true
  try {
    const target = editingKey.value
    if (target === null) {
      const created = await createKey({
        label: issueLabel.value,
        policy: toPolicyInput(form.value),
      })
      // 失效后**等**刷新完成再关框：本票验收里的「保存后立刻回显」就是这个等待。
      await queryClient.invalidateQueries({ queryKey: KEYS_QUERY_KEY })
      ElMessage.success(`已签发：${created.key_masked}`)
      closeEditor()
      return
    }

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
    // 失败时保留对话框：错误文案已经说明原因，用户多半只想改一个值再试。
    saveError.value = errorText(error)
  } finally {
    saving.value = false
  }
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

// —— 获取密钥（默认只给 masked；二次确认；看完不缓存） ——

const revealTarget = ref<KeyData | null>(null)
const revealConfirmOpen = ref(false)
/** 只在「原文」弹窗打开期间存在；关闭即置空，不留任何第二份副本。 */
const revealed = ref<{ keyId: string; value: string } | null>(null)

const revealConfirmMessage = computed(() => {
  const key = revealTarget.value
  if (key === null) return ''
  return (
    `《${keyLabel(key)}》的密钥会以明文显示在当前页面（列表里只显示 ${key.key_masked}）。` +
    '请勿录屏或截图；关闭这个窗口后页面不再保留它。'
  )
})

function askReveal(row: unknown): void {
  if (!isKeyRow(row)) return
  revealTarget.value = row
  revealConfirmOpen.value = true
}

function revealNow(): void {
  const key = revealTarget.value
  revealConfirmOpen.value = false
  revealTarget.value = null
  if (key === null) return
  revealed.value = { keyId: key.key_id, value: key.key_value }
}

function hideReveal(): void {
  // 「看完不缓存」的唯一实现点：关窗即丢弃，不写 storage、不留在别的 ref。
  revealed.value = null
}

const revealOpen = computed({
  get: () => revealed.value !== null,
  set: (open: boolean) => {
    if (!open) hideReveal()
  },
})

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
    subtitle="一把 Key 绑定一套密钥配置：能查哪些库、开哪些能力、多大的限额、默认档位都写在配置里。改配置会立刻作用于用它发起的下一次检索，不必重签 Key。"
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

      <!-- 空态说下一步，不堆一句「还没有任何 Key」就完事 -->
      <div v-else-if="keys.length === 0" class="py-8 text-center">
        <p class="text-[15px] font-semibold text-ink">还没有 Key</p>
        <p class="mx-auto mt-2 max-w-md text-sm leading-relaxed text-muted">
          签发一把之后，去「检索调试台」选中它就能用它的原文打真实接口。
        </p>
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
            <!-- 第一层：名字 + 状态 + 短 id -->
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-sm font-semibold text-ink">{{ keyLabel(row) }}</span>
              <span
                class="inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium"
                :style="statusPillStyle(row.status)"
              >
                {{ keyStatusLabel(row.status) }}
              </span>
              <span class="font-mono text-[11px] text-muted" :title="row.key_id">
                {{ shortKeyId(row) }}
              </span>
            </div>

            <!-- 第二层：配置摘要拆成 chips，换行只在块之间发生 -->
            <div class="flex flex-wrap items-center gap-1.5">
              <span
                v-for="chip in policyChips(row)"
                :key="chip"
                class="inline-flex items-center rounded-full border border-line bg-surface-muted px-2 py-0.5 text-[11px] text-slate-600"
              >
                {{ chip }}
              </span>
            </div>
          </div>

          <div class="flex shrink-0 flex-wrap items-center gap-1">
            <el-button
              link
              type="primary"
              size="small"
              :disabled="row.policy === null"
              @click="openEdit(row)"
            >
              编辑配置
            </el-button>
            <el-button link type="primary" size="small" @click="askReveal(row)">
              获取密钥
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
      width="760px"
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
            placeholder="例如：集成 A —— 只给人看，鉴权不读它"
          />
        </el-form-item>
      </el-form>
      <p v-else class="mb-3 text-xs text-slate-500">
        正在编辑《{{ keyLabel(editingKey) }}》（<code class="font-mono">{{ editingKey.key_masked }}</code>）
        的密钥配置；改完保存后，用它发起的下一次检索立即按新配置执行。
      </p>

      <KeyPolicyEditor
        v-model="form"
        :allowed-profiles="allowedProfiles"
        :kb-options="kbOptions"
        :plan-hint="planHint"
        :presets="profilePresets"
      />

      <p v-if="keysLoaded && allowedProfiles.length === 0" class="mt-3 text-xs text-amber-700">
        还没读到套餐的档位白名单（`me.plan.allowed_profiles` 为空）：四个档位都会置灰，
        先确认账号确实属于一个租户。
      </p>

      <div
        v-if="saveError !== ''"
        class="mt-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
      >
        {{ saveError }}
      </div>

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

    <!-- 获取密钥的二次确认 -->
    <ConfirmDialog
      v-model="revealConfirmOpen"
      title="获取密钥"
      :message="revealConfirmMessage"
      confirm-text="获取密钥"
      @confirm="revealNow"
    />

    <!-- 明文弹窗：关掉即丢弃（`revealOpen` 的 setter 会清空 revealed） -->
    <el-dialog v-model="revealOpen" append-to-body title="密钥明文（关闭后不再保留）" width="560px">
      <el-input
        :model-value="revealed?.value ?? ''"
        readonly
        class="font-mono"
        placeholder=""
      />
      <p class="mt-2 text-xs text-slate-500">
        不要截图或分享；这把 Key 的有效期与状态以列表里的状态列为准。
      </p>
    </el-dialog>
  </section>
</template>
