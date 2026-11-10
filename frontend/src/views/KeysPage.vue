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

function policySummary(row: unknown): string {
  const key = row as KeyData
  const policy: KeyPolicyData | null = key.policy
  if (policy === null) return '—（配置缺失）'
  const capabilities: string[] = []
  if (policy.research_allowed) capabilities.push('research')
  if (policy.hybrid_allowed) capabilities.push('hybrid')
  if (policy.rerank_allowed) capabilities.push('rerank')
  if (policy.query_rewrite_allowed) capabilities.push('query_rewrite')
  const capabilityText = capabilities.length === 0 ? '无额外能力' : capabilities.join(' / ')
  return `${policy.profile} · ${capabilityText} · 白名单 ${policy.allowed_kb_ids.length} 个库 · ${policy.retrieve_qps} 次/秒 / ${policy.retrieve_daily} 次/天`
}

function keyLabel(row: unknown): string {
  const key = row as KeyData
  return key.label === null || key.label.trim() === '' ? '（未命名）' : key.label
}

/** 状态是字符串（`active` 等）；本页只做「生效中 / 其它」两档，不编造一套状态机。 */
function statusText(row: unknown): string {
  const key = row as KeyData
  return key.status === 'active' ? '生效中' : key.status
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

// —— 显示原文（默认 masked；二次确认；看完不缓存） ——

const revealTarget = ref<KeyData | null>(null)
const revealConfirmOpen = ref(false)
/** 只在「原文」弹窗打开期间存在；关闭即置空，不留任何第二份副本。 */
const revealed = ref<{ keyId: string; value: string } | null>(null)

const revealConfirmMessage = computed(() => {
  const key = revealTarget.value
  if (key === null) return ''
  return (
    `这把 Key 的原文会以明文显示在当前页面（${key.key_masked}）。` +
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
  <section class="space-y-4">
    <el-card shadow="never">
      <template #header>
        <div class="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-sm font-semibold text-slate-800">API Key</h2>
            <p class="mt-1 text-xs text-slate-500">
              一把 Key 绑定一套密钥配置：能查哪些库、开哪些能力、多大的限额、默认档位都写在
              配置里。改配置会立刻作用于用它发起的下一次检索。
            </p>
          </div>
          <el-button type="primary" @click="openIssue">签发新 Key</el-button>
        </div>
      </template>

      <p v-if="keysPending" class="text-sm text-slate-400">正在读取 Key 列表…</p>
      <div
        v-else-if="keysError !== ''"
        class="rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
      >
        {{ keysError }}
      </div>
      <p v-else-if="keys.length === 0" class="text-sm text-slate-500">
        还没有任何 Key。签发一把之后，去「检索调试台」选中它就能用它的原文打真实接口。
      </p>

      <el-table v-else :data="keys" row-key="key_id" class="w-full">
        <el-table-column label="备注名" min-width="160">
          <template #default="{ row }">
            <span class="text-sm font-medium text-slate-800">{{ keyLabel(row) }}</span>
            <p class="mt-0.5 text-xs text-slate-400">{{ row.key_id }}</p>
          </template>
        </el-table-column>

        <el-table-column label="Key" min-width="200">
          <template #default="{ row }">
            <div class="flex flex-wrap items-center gap-2">
              <code class="font-mono text-xs text-slate-700">{{ row.key_masked }}</code>
              <el-button link type="primary" size="small" @click="askReveal(row)">
                显示原文
              </el-button>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <span
              class="inline-flex items-center rounded px-2 py-0.5 text-xs font-medium"
              :style="
                row.status === 'active'
                  ? { backgroundColor: '#D1FAE5', color: '#065F46' }
                  : { backgroundColor: '#F3F4F6', color: '#6B7280' }
              "
            >
              {{ statusText(row) }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="配置摘要" min-width="260">
          <template #default="{ row }">
            <span class="text-xs text-slate-600">{{ policySummary(row) }}</span>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="180">
          <template #default="{ row }">
            <div class="flex flex-wrap items-center gap-3 text-sm">
              <el-button
                link
                type="primary"
                size="small"
                :disabled="row.policy === null"
                @click="openEdit(row)"
              >
                编辑配置
              </el-button>
              <el-button link type="danger" size="small" @click="askDelete(row)">删除</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 签发 / 编辑 -->
    <el-dialog
      v-model="editorOpen"
      :title="editorTitle"
      width="720px"
      :close-on-click-modal="!saving"
      :close-on-press-escape="!saving"
      :show-close="!saving"
    >
      <el-form v-if="editingKey === null" label-position="top">
        <el-form-item label="备注名（label，可选；鉴权不读它）">
          <el-input v-model="issueLabel" maxlength="128" show-word-limit placeholder="例如：集成 A" />
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

    <!-- 显示原文的二次确认 -->
    <ConfirmDialog
      v-model="revealConfirmOpen"
      title="显示 Key 原文"
      :message="revealConfirmMessage"
      confirm-text="显示原文"
      @confirm="revealNow"
    />

    <!-- 原文弹窗：关掉即丢弃（`revealOpen` 的 setter 会清空 revealed） -->
    <el-dialog v-model="revealOpen" title="Key 原文（关闭后不再保留）" width="560px">
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
