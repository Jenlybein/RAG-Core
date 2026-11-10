<script setup lang="ts">
/**
 * 模型配置页：列出全部模型接入、增删改、改角色指向、二次确认后显示一次明文 Key。
 *
 * 五处刻意的写法，都是票面点名的：
 *
 * 1. **列表与表单里的 Key 永远 masked**。明文只从「显示原文」弹窗出一次，关闭即丢弃，
 *    不写 localStorage / sessionStorage / URL——`api_key` 是能直接花掉的钱。
 * 2. **删除被引用的配置由后端 20015 兜底**：`20015` 的通用文案是「参数非法」，会把
 *    后端那句「先把 fast/strong 的指向改掉」吃掉，所以这里单独取 `ApiError.backendMsg`
 *    展示原文并指明下一步。前端不从 `roles` 反推「能不能删」——那等于把后端的判定抄一遍。
 * 3. **角色指向保存走配置中台**（`PATCH /console/settings?dry_run=false`，body
 *    `{values: {"model.role.fast": <id 或 "">}}`）：指针不是 `model_configs` 的列，它
 *    属于运行时配置，这样一次改指向和改别的热配置走同一条审计与缓存失效路径。
 * 4. **「不指向（回落 .env）」是下拉里的一项**：它是管理员撤销指向、回到单供应商的
 *    唯一入口，不是一个要藏起来的空值。
 * 5. **写清楚「重排跟随 fast，没有独立的重排模型配置」**：重排一期包着 `LLMProvider`
 *    （ADR-0002），不写这句，用户会去找一个不存在的下拉。
 */
import { useQuery, useQueryClient } from '@tanstack/vue-query'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import { ApiError } from '../../lib/envelope'
import {
  MODEL_CONFIGS_QUERY_KEY,
  createModelConfig,
  deleteModelConfig,
  fetchModelConfigs,
  revealModelConfigSecret,
  updateModelConfig,
} from '../../services/consoleModels'
import { updateSettings } from '../../services/consoleSettings'
import type { ModelConfigCreateRequest, ModelConfigData, ModelConfigPatchRequest, ModelRole } from '../../types/modelConfig'
import { errorText } from '../../utils/errorText'
import {
  MODEL_ROLES,
  MODEL_ROLE_LABELS,
  MODEL_VENDORS,
  referencedRolesOf,
  roleOptions,
  vendorLabel,
} from '../../utils/modelsPage'
import { ROLE_POINTER_PREFIX } from '../../utils/settingsTier'

const queryClient = useQueryClient()

const configsQuery = useQuery({
  queryKey: MODEL_CONFIGS_QUERY_KEY,
  queryFn: fetchModelConfigs,
})

const configs = computed(() => configsQuery.data.value?.items ?? [])
const loading = computed(() => configsQuery.isPending.value)
const loadError = computed(() =>
  configsQuery.isError.value ? errorText(configsQuery.error.value) : '',
)

/** 下拉选项只在配置列表变化时重算；「不指向」永远是第一项。 */
const options = computed(() => roleOptions(configs.value))

// ───────────────────────────────────────────────── 角色指向

/**
 * 当前指向与编辑中的指向分成两份：`pointers` 是服务端事实（每次列表刷新重算），
 * `roleDraft` 是管理员这次要保存的值。混成一份会在列表刷新时把正在改的选择冲掉。
 */
const pointers = reactive<Record<ModelRole, string>>({ fast: '', strong: '' })
const roleDraft = reactive<Record<ModelRole, string>>({ fast: '', strong: '' })
const rolesLoaded = ref(false)
const savingRoles = ref(false)

/**
 * 从列表行反推当前指向：后端已经在每行上算好 `roles`，这里只按角色归位。
 * 指针若指向一条已被手工删掉的行，则没有任何行声明这个角色 → 视为「不指向」
 * （运行时本来也会回落到 `.env`，与后端的处理一致）。
 */
function pointersFrom(items: readonly ModelConfigData[]): Record<ModelRole, string> {
  const result: Record<ModelRole, string> = { fast: '', strong: '' }
  for (const config of items) {
    for (const role of referencedRolesOf(config)) {
      result[role] = config.id
    }
  }
  return result
}

// 首次载入与保存后刷新时同步；管理员正在改的那一次不被后台 refetch 冲掉。
// 监听 query 的 data 而不是 `configs`：后者在「还没载入」时也返回 `[]`，拿它 immediate
// 同步会把「空列表」当成事实并锁住 `rolesLoaded`，随后真实数据到达时反而不同步了。
watch(
  () => configsQuery.data.value,
  (data) => {
    if (data === undefined || rolesLoaded.value) return
    const current = pointersFrom(data.items)
    Object.assign(pointers, current)
    Object.assign(roleDraft, current)
    rolesLoaded.value = true
  },
  { immediate: true },
)

/** 这次要提交的指针键：只发真的变了的角色，没动的角色不写库也不进审计。 */
const roleChanges = computed<Record<string, string>>(() => {
  const values: Record<string, string> = {}
  for (const role of MODEL_ROLES) {
    if (roleDraft[role] !== pointers[role]) {
      values[`${ROLE_POINTER_PREFIX}${role}`] = roleDraft[role]
    }
  }
  return values
})

const roleDirty = computed(() => Object.keys(roleChanges.value).length > 0)

async function saveRoles(): Promise<void> {
  if (!roleDirty.value) {
    ElMessage.info('角色指向没有改动，未发送请求')
    return
  }

  savingRoles.value = true
  try {
    // dry_run=false 才是真写：这一页没有影响面可预览（改指向不会让谁失去能力，
    // 只是换一个模型接入），所以不必像套餐页那样先试算。
    await updateSettings(roleChanges.value, false)
    ElMessage.success('已保存：下一次模型装配即按新指向（读指针不走缓存，不需要重启）')
    rolesLoaded.value = false
    await queryClient.invalidateQueries({ queryKey: MODEL_CONFIGS_QUERY_KEY })
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    savingRoles.value = false
  }
}

// ───────────────────────────────────────────────── 新增 / 编辑

const dialogVisible = ref(false)
const dialogMode = ref<'create' | 'edit'>('create')
const editingConfig = ref<ModelConfigData | null>(null)
const saving = ref(false)

function emptyDraft(): ModelConfigCreateRequest {
  return { label: '', provider: MODEL_VENDORS[0], base_url: '', model: '', api_key: '' }
}

const draft = reactive<ModelConfigCreateRequest>(emptyDraft())

function openCreate(): void {
  Object.assign(draft, emptyDraft())
  editingConfig.value = null
  dialogMode.value = 'create'
  dialogVisible.value = true
}

function openEdit(config: ModelConfigData): void {
  Object.assign(draft, {
    label: config.label,
    provider: config.provider,
    base_url: config.base_url,
    model: config.model,
    // **留空 = 不改**：把列表里的 masked 值预填进表单再提交，会把真实 Key 覆盖成
    // "sk-…abcd" 这个打码串，下次调用直接 401。
    api_key: '',
  })
  editingConfig.value = config
  dialogMode.value = 'edit'
  dialogVisible.value = true
}

function validateDraft(): string | null {
  if (draft.label.trim() === '') return '备注名不能为空（它用来区分生产与备用）'
  if (draft.model.trim() === '') return '模型名不能为空'
  if (!MODEL_VENDORS.includes(draft.provider)) {
    return '供应商只能从预设里选；自定义端点请填 base_url'
  }
  return null
}

/**
 * 编辑时只发真正动过的字段。
 *
 * `api_key` 单独处理：留空表示「不改」，所以不放进 patch；填了才发（明确输入空串的
 * 方式不提供——「清掉 Key」不是一个常用动作，要做就明确填一个占位以外的东西）。
 */
function buildPatch(config: ModelConfigData): ModelConfigPatchRequest {
  const patch: ModelConfigPatchRequest = {}
  const label = draft.label.trim()
  const model = draft.model.trim()
  const baseUrl = draft.base_url.trim()
  const apiKey = draft.api_key.trim()

  if (label !== config.label) patch.label = label
  if (draft.provider !== config.provider) patch.provider = draft.provider
  if (model !== config.model) patch.model = model
  if (baseUrl !== config.base_url) patch.base_url = baseUrl
  if (apiKey !== '') patch.api_key = apiKey

  return patch
}

async function saveConfig(): Promise<void> {
  const invalid = validateDraft()
  if (invalid !== null) {
    ElMessage.warning(invalid)
    return
  }

  saving.value = true
  try {
    if (dialogMode.value === 'create') {
      await createModelConfig({
        label: draft.label.trim(),
        provider: draft.provider,
        base_url: draft.base_url.trim(),
        model: draft.model.trim(),
        api_key: draft.api_key.trim(),
      })
      ElMessage.success('已新增：角色指向它之后，下一次模型装配即使用这条配置')
    } else {
      const config = editingConfig.value
      if (config === null) return
      const patch = buildPatch(config)
      if (Object.keys(patch).length === 0) {
        ElMessage.info('没有改动，未发送请求')
        return
      }
      await updateModelConfig(config.id, patch)
      ElMessage.success('已保存：下一次模型装配即按新配置')
    }

    dialogVisible.value = false
    rolesLoaded.value = false
    await queryClient.invalidateQueries({ queryKey: MODEL_CONFIGS_QUERY_KEY })
  } catch (error) {
    ElMessage.error(errorText(error))
  } finally {
    saving.value = false
  }
}

// ───────────────────────────────────────────────── 删除

async function remove(config: ModelConfigData): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除模型配置「${config.label}」？删除不可撤销。` +
        '若仍有 fast / strong 指向它，后端会拒绝并提示先改指向。',
      '删除模型配置',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  try {
    await deleteModelConfig(config.id)
    ElMessage.success('模型配置已删除')
    rolesLoaded.value = false
    await queryClient.invalidateQueries({ queryKey: MODEL_CONFIGS_QUERY_KEY })
  } catch (error) {
    if (error instanceof ApiError && error.code === 20015) {
      // 通用 20015 文案会把后端原文吃掉，这里把原文原样展示并给出下一步。
      await ElMessageBox.alert(
        `后端原文：${error.backendMsg}\n\n` +
          '先改角色指向：在下方「模型角色」区块把 fast / strong 改成别的配置，' +
          '或选「不指向（回落 .env）」，再回来删除。',
        '仍被角色引用，不能删除',
        { type: 'warning', confirmButtonText: '知道了' },
      )
      return
    }
    ElMessage.error(errorText(error))
  }
}

// ───────────────────────────────────────────────── 显示原文

const secretVisible = ref(false)
const secretValue = ref('')
const secretConfig = ref<ModelConfigData | null>(null)

/**
 * 明文只活在 `secretValue` 这一个组件内变量里，关闭弹窗立刻清空；不写任何存储、
 * 不进 URL、不进日志。后端每次调用都会留一条 `model.reveal` 审计。
 */
async function reveal(config: ModelConfigData): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认显示「${config.label}」的 Key 明文？` +
        '明文只显示这一次，查看动作会写进审计日志。',
      '显示原文',
      { type: 'warning', confirmButtonText: '显示原文', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  try {
    const data = await revealModelConfigSecret(config.id)
    secretConfig.value = config
    secretValue.value = data.api_key
    secretVisible.value = true
  } catch (error) {
    ElMessage.error(errorText(error))
  }
}

function onSecretClosed(): void {
  // 关闭即丢弃：不保留任何一份明文引用。
  secretValue.value = ''
  secretConfig.value = null
}

async function refresh(): Promise<void> {
  rolesLoaded.value = false
  await configsQuery.refetch()
}
</script>

<template>
  <section class="rounded-lg border border-line bg-white p-6">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-base font-semibold text-slate-800">模型配置</h2>
        <p class="mt-1 text-sm text-slate-500">
          模型接入是平台级配置（不属于任何租户）：一条配置里写着用哪家模型、哪个端点、哪把 Key。
          改完下一次装配即生效，不需要重启。
        </p>
        <p class="mt-1 text-xs text-slate-400">
          重排跟随 <span class="font-mono">fast</span>，没有独立的重排模型配置：重排一期包着
          LLMProvider（ADR-0002），所以这里没有「重排模型」这个下拉。
        </p>
      </div>
      <div class="flex items-center gap-2">
        <span v-if="configs.length > 0" class="text-sm text-slate-500">
          共 {{ configs.length }} 条
        </span>
        <el-button size="small" @click="openCreate">新增模型配置</el-button>
        <el-button size="small" :loading="configsQuery.isFetching.value" @click="refresh">
          刷新
        </el-button>
      </div>
    </div>

    <div v-if="loading" class="py-10 text-center text-sm text-slate-500">加载模型配置…</div>

    <div
      v-else-if="loadError !== ''"
      class="mt-4 rounded border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800"
    >
      <p>模型配置读取失败：{{ loadError }}</p>
      <el-button class="mt-2" size="small" @click="refresh">重试</el-button>
    </div>

    <div v-else class="mt-4 overflow-x-auto">
      <table class="min-w-full text-sm">
        <thead class="bg-[#F8F9FA] text-[#6B7280]">
          <tr>
            <th class="px-3 py-2 text-left font-medium">备注名</th>
            <th class="px-3 py-2 text-left font-medium">供应商</th>
            <th class="px-3 py-2 text-left font-medium">模型</th>
            <th class="px-3 py-2 text-left font-medium">接入地址</th>
            <th class="px-3 py-2 text-left font-medium">Key</th>
            <th class="px-3 py-2 text-left font-medium">被哪些角色引用</th>
            <th class="px-3 py-2 text-left font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="config in configs" :key="config.id" class="border-t border-line align-top">
            <td class="px-3 py-3">
              <p class="font-medium text-slate-800">{{ config.label }}</p>
              <p class="font-mono text-xs text-slate-400">{{ config.id }}</p>
            </td>
            <td class="px-3 py-3 text-slate-600">{{ vendorLabel(config.provider) }}</td>
            <td class="px-3 py-3 font-mono text-slate-600">{{ config.model }}</td>
            <td class="px-3 py-3 text-slate-600">
              <span v-if="config.base_url === ''" class="text-slate-400">默认地址</span>
              <span v-else class="break-all font-mono text-xs">{{ config.base_url }}</span>
            </td>
            <td class="px-3 py-3">
              <!-- 列表里永远是后端给的回显形态（sk-…abcd），本页不存也不展示明文。 -->
              <span class="font-mono text-xs text-slate-500">
                {{ config.api_key === '' ? '—（未填 Key）' : config.api_key }}
              </span>
            </td>
            <td class="px-3 py-3">
              <div class="flex flex-wrap gap-1">
                <span
                  v-for="role in referencedRolesOf(config)"
                  :key="role"
                  class="rounded bg-[#D1FAE5] px-2 py-0.5 text-xs text-[#065F46]"
                >
                  {{ role }}
                </span>
                <span v-if="referencedRolesOf(config).length === 0" class="text-xs text-slate-400">
                  —
                </span>
              </div>
            </td>
            <td class="px-3 py-3">
              <div class="flex flex-wrap items-center gap-2">
                <el-button size="small" @click="openEdit(config)">编辑</el-button>
                <el-button size="small" @click="reveal(config)">显示原文</el-button>
                <el-button size="small" type="danger" plain @click="remove(config)">
                  删除
                </el-button>
              </div>
            </td>
          </tr>

          <tr v-if="configs.length === 0">
            <td colspan="7" class="px-3 py-8 text-center text-sm text-slate-500">
              还没有模型配置。没有配置时运行时回落到 <span class="font-mono">.env</span>
              的单供应商（离线 / CI 的行为不变）。
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="mt-8 rounded border border-line p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h3 class="text-sm font-semibold text-slate-800">模型角色</h3>
          <p class="mt-1 text-xs text-slate-500">
            角色指向保存在运行时配置里（键名
            <span class="font-mono">model.role.fast</span> /
            <span class="font-mono">model.role.strong</span>，值是上面某条配置的 id）。
            选「不指向（回落 .env）」即可撤销指向，回到 .env 的单供应商。
          </p>
        </div>
        <el-button
          type="primary"
          size="small"
          :loading="savingRoles"
          :disabled="!roleDirty"
          @click="saveRoles"
        >
          保存角色指向
        </el-button>
      </div>

      <div class="mt-4 grid gap-4 sm:grid-cols-2">
        <label v-for="role in MODEL_ROLES" :key="role" class="block text-sm">
          <span class="mb-1 block text-slate-500">{{ MODEL_ROLE_LABELS[role] }}</span>
          <el-select v-model="roleDraft[role]" class="w-full" :disabled="options.length === 1">
            <el-option
              v-for="option in options"
              :key="option.value"
              :value="option.value"
              :label="option.label"
            />
          </el-select>
        </label>
      </div>

      <p v-if="options.length === 1" class="mt-2 text-xs text-slate-400">
        还没有任何模型配置，只能选「不指向（回落 .env）」。
      </p>
      <p v-if="!roleDirty" class="mt-2 text-xs text-slate-400">
        当前指向与服务端一致；改了下拉后按钮才会可用。
      </p>
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新增模型配置' : '编辑模型配置'"
      width="560px"
      :close-on-click-modal="false"
    >
      <div class="space-y-4 text-sm">
        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-slate-500">备注名</span>
          <el-input v-model="draft.label" maxlength="128" placeholder="例如：生产 / 备用" />
        </label>

        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-slate-500">供应商</span>
          <el-select v-model="draft.provider" class="flex-1">
            <el-option
              v-for="provider in MODEL_VENDORS"
              :key="provider"
              :value="provider"
              :label="vendorLabel(provider)"
            />
          </el-select>
        </label>
        <p class="-mt-2 pl-24 text-xs text-slate-400">
          只支持这六家。自定义端点也选其中一家，把下面的接入地址填成自建网关（后端按供应商名
          索引装配知识，任意字符串会让第一次调用才报错）。
        </p>

        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-slate-500">模型名</span>
          <el-input v-model="draft.model" maxlength="128" placeholder="例如：deepseek-v4-pro" />
        </label>

        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-slate-500">接入地址</span>
          <el-input
            v-model="draft.base_url"
            maxlength="255"
            placeholder="留空 = 用该供应商的默认地址"
          />
        </label>

        <label class="flex items-center gap-3">
          <span class="w-24 shrink-0 text-slate-500">Key</span>
          <el-input
            v-model="draft.api_key"
            type="password"
            show-password
            maxlength="255"
            :placeholder="
              dialogMode === 'edit'
                ? `留空 = 不修改（当前 ${editingConfig?.api_key || '未填'}）`
                : '可留空，之后再填'
            "
          />
        </label>
        <p class="-mt-2 pl-24 text-xs text-slate-400">
          编辑时留空表示不改；列表与表单里永远不展示明文，要看原文请用「显示原文」。
        </p>

        <p class="rounded bg-[#F8F9FA] px-3 py-2 text-xs text-slate-500">
          供应商名必须属于六家预设（后端会拒未知值）；模型名与接入地址决定实际调用，
          保存后下一次模型装配即生效。
        </p>
      </div>

      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveConfig">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="secretVisible"
      title="Key 明文（只显示这一次）"
      width="640px"
      :close-on-click-modal="false"
      @closed="onSecretClosed"
    >
      <div class="space-y-3 text-sm">
        <p class="text-slate-600">
          配置「{{ secretConfig?.label ?? '' }}」的明文 Key：
        </p>
        <div
          v-if="secretValue !== ''"
          class="break-all rounded border border-amber-200 bg-amber-50 px-3 py-2 font-mono text-xs text-slate-800"
        >
          {{ secretValue }}
        </div>
        <p v-else class="rounded bg-[#F8F9FA] px-3 py-2 text-sm text-slate-500">
          这条配置还没有填 Key（明文为空）。
        </p>
        <p class="text-xs text-slate-400">
          关闭弹窗即丢弃这份明文，本页不把它写进任何存储。本次查看已在审计日志里留了一条
          <span class="font-mono">model.reveal</span>。
        </p>
      </div>
      <template #footer>
        <el-button type="primary" @click="secretVisible = false">关闭并丢弃</el-button>
      </template>
    </el-dialog>
  </section>
</template>
