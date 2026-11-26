<script setup lang="ts">
/**
 * `/keys`：本租户的 Key 列表 + 签发 + 编辑密钥配置 + 删除。
 *
 * 页面自己只做三件事：什么时候请求（`useQuery` / 直接 await 的写操作）、弹哪个框、
 * 把结果放到哪。请求形状归 `services/keyService.ts`，判定（哪些字段算改动、预设怎么填）
 * 归 `utils/keyPolicy.ts`——组件里不再写第二份。
 *
 * 两条与安全相关的取舍（issue #92，ADR-0004 修订版）：
 *
 * - **列表里只有打码形态**。库里只存 scrypt 哈希与末 5 位，原文只在**签发那一刻**的
 *   响应里出现过一次（`key_plaintext`），所以页面没有「显示原文」这个动作可做；能做的是
 *   在签发成功后当场把那一串联交给人（复制 / 直接去调试台试一把），弹窗关掉就不再保留；
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
  MAX_UNIT_CHARS_DEFAULT,
  TOP_K_DEFAULT,
  type IssuedKeyData,
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
      // 原文只在这一个响应里存在过一次：先把它交给弹窗，关框即丢弃。
      issuedKey.value = created
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

// —— 签发成功后的「只此一次」 ——

/**
 * 刚签发的那个响应。**它是原文在本页唯一的副本**：库里没有、列表接口也不会再给。
 *
 * 关窗即置空——不写 storage、不进 URL、不留在别的 ref。想再用只能重签一把，所以弹窗里
 * 给的是「复制」和「去调试台试用」两个出口，而不是一句「请妥善保存」。
 */
const issuedKey = ref<IssuedKeyData | null>(null)
/** 复制成功/失败的即时反馈（按钮文案换一下，比 toast 更近）。 */
const copied = ref(false)

const issuedOpen = computed({
  get: () => issuedKey.value !== null,
  set: (open: boolean) => {
    if (!open) closeIssued()
  },
})

function closeIssued(): void {
  issuedKey.value = null
  copied.value = false
}

async function copyIssuedKey(): Promise<void> {
  const key = issuedKey.value
  if (key === null) return
  try {
    await navigator.clipboard.writeText(key.key_plaintext)
    copied.value = true
  } catch {
    // 剪贴板被浏览器拒绝（非 https、缺权限）时不要假装成功：让用户自己从输入框里选。
    ElMessage.error('复制失败：请手动选中输入框里的内容复制')
  }
}

/**
 * 「去调试台试用」：把这串交给检索页的请求层，然后跳过去。
 *
 * 只写进内存（`lib/activeKey`）——调试台那台机器上的 Key 一律由用户自己粘贴，这里是
 * 唯一一次「不用再粘」的例外，因为它就发生在签发之后的同一屏里。
 */
function goToDebug(): void {
  const key = issuedKey.value
  if (key === null) return
  retrieveStore.selectKey({
    keyId: key.key_id,
    keyValue: key.key_plaintext,
    allowedKbIds: key.policy?.allowed_kb_ids ?? [],
  })
  closeIssued()
  void router.push('/retrieve')
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
            </div>

            <!-- 第二层：配置摘要拆成 chips，换行只在块之间发生 -->
            <div class="flex flex-wrap items-center gap-1.5">
              <span v-for="chip in policyChips(row)" :key="chip" class="chip">
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

    <!--
      签发成功后的一次性弹窗：完整 Key 只在这里出现。关掉之后库里只剩 scrypt 哈希，
      所以出口给两个——复制走，或者直接去调试台拿它打一次真实接口。
    -->
    <el-dialog
      v-model="issuedOpen"
      append-to-body
      title="这把 Key 只显示这一次"
      width="640px"
    >
      <p class="mb-2 text-sm text-ink">
        服务端只保存它的哈希与末 5 位（列表里显示为
        <code class="font-mono">{{ issuedKey?.key_masked }}</code>）。关掉这个窗口后就再也
        取不回来了，丢了只能重新签发一把。
      </p>
      <el-input
        :model-value="issuedKey?.key_plaintext ?? ''"
        readonly
        type="textarea"
        :rows="3"
        class="font-mono"
        @focus="($event.target as HTMLTextAreaElement | null)?.select()"
      />
      <template #footer>
        <div class="flex flex-wrap justify-end gap-2">
          <el-button @click="closeIssued">我已保存</el-button>
          <el-button type="primary" plain @click="copyIssuedKey">
            {{ copied ? '已复制' : '复制' }}
          </el-button>
          <el-button type="primary" @click="goToDebug">去调试台试用</el-button>
        </div>
      </template>
    </el-dialog>
  </section>
</template>
