<script setup lang="ts">
/**
 * 密钥配置编辑器：五组字段 + 只在 `custom` 档出现的手工参数。
 *
 * 这个组件的职责边界刻意很清楚：它只「渲染表单并改父级传进来的那个 form 对象」，
 * **不发请求、不做自洽校验**。校验与保存由 `KeysPage.vue` 调 `keyService` 完成，
 * 判定（哪些档位被套餐挡住、哪些字段算改动）由 `utils/keyPolicy.ts` 的纯函数给出——
 * 组件里再写一份就会出现「界面允许保存、后端 20015 拒绝」的分叉。
 *
 * 为什么 `custom` 那组用 `v-if` 而不是 `v-show`：`v-show` 只是隐藏，输入框还挂在表单上；
 * 用户切回命名档位后，那批已经失效的输入仍会被 `buildPolicyPatch` 当作「改过的值」发出去。
 * `v-if` 在切走时直接卸载，`buildPolicyPatch` 那边也有 `visibleCustomOptions` 兜底，两道锁。
 */
import { computed } from 'vue'

import {
  CUSTOM_OPTION_MODES,
  CUSTOM_TOP_K_MAX,
  CUSTOM_TOP_K_MIN,
  KEY_PROFILE_LABELS,
  KEY_PROFILES,
  type KbOption,
  type PolicyForm,
} from '../types/key'
import { disabledProfiles, visibleCustomOptions } from '../utils/keyPolicy'

/** 父级持有的表单对象：子组件直接改它，保存时读的是同一份状态。 */
const form = defineModel<PolicyForm>({ required: true })

const props = defineProps<{
  /** `GET /console/me` 里套餐的 `allowed_profiles`：下拉只从这一份白名单里放行。 */
  allowedProfiles: string[]
  /** 控制面树里的库：`allowed_kb_ids` 的候选来源。 */
  kbOptions: KbOption[]
  /** 套餐的能力/限额提示；缺省（例如还没读到 `me`）时只显示不块状的说明。 */
  planHint: {
    hybridAllowed: boolean
    rerankAllowed: boolean
    queryRewriteAllowed: boolean
    researchAllowed: boolean
    retrieveQps: number
    retrieveDaily: number
  } | null
}>()

const blockedProfiles = computed(() => new Set(disabledProfiles(props.allowedProfiles)))

const blockedProfileReason = '当前套餐不支持该档位'

/** 套餐允许的能力清单，给开关下面那句提示用；为空时不摆一句空话。 */
const planCapabilityText = computed(() => {
  const plan = props.planHint
  if (plan === null) return ''
  const allowed: string[] = []
  if (plan.researchAllowed) allowed.push('research')
  if (plan.hybridAllowed) allowed.push('hybrid')
  if (plan.rerankAllowed) allowed.push('rerank')
  if (plan.queryRewriteAllowed) allowed.push('query_rewrite')
  return allowed.length === 0 ? '当前套餐没有开放任何额外能力' : allowed.join(' / ')
})

const qpsHint = computed(() =>
  props.planHint === null ? null : `套餐上限 ${props.planHint.retrieveQps} 次/秒`,
)
const dailyHint = computed(() =>
  props.planHint === null ? null : `套餐上限 ${props.planHint.retrieveDaily} 次/天`,
)
</script>

<template>
  <el-form label-position="top" class="space-y-2">
    <!-- 第一组：数据范围 -->
    <section>
      <h3 class="mb-1 text-sm font-semibold text-slate-800">数据范围</h3>
      <el-form-item label="允许检索的知识库（allowed_kb_ids）">
        <el-select
          v-model="form.allowed_kb_ids"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          class="w-full"
          placeholder="留空表示什么都没圈定（不是全部）"
        >
          <el-option
            v-for="kb in kbOptions"
            :key="kb.kb_id"
            :label="`${kb.name}（${kb.kb_id}）`"
            :value="kb.kb_id"
          />
        </el-select>
        <p class="mt-1 text-xs text-slate-500">
          只有列表里的库能被这把 Key 检索；空列表表示「什么都没圈定」，不是「全部」。
        </p>
      </el-form-item>
    </section>

    <!-- 第二组：能力开关 -->
    <section>
      <h3 class="mb-1 text-sm font-semibold text-slate-800">能力开关</h3>
      <div class="grid gap-2 sm:grid-cols-2">
        <el-checkbox v-model="form.research_allowed">research（研究型检索）</el-checkbox>
        <el-checkbox v-model="form.hybrid_allowed">hybrid（向量 + 关键词混合检索）</el-checkbox>
        <el-checkbox v-model="form.rerank_allowed">rerank（结果重排）</el-checkbox>
        <el-checkbox v-model="form.query_rewrite_allowed">
          query_rewrite（查询改写）
        </el-checkbox>
      </div>
      <p class="mt-1 text-xs text-slate-500">
        配置只能在套餐上限内分配；命名档位还需要它展开所需的能力全部打开，否则保存会被拒。
        <span v-if="planCapabilityText !== ''">当前套餐允许：{{ planCapabilityText }}。</span>
      </p>
    </section>

    <!-- 第三组：限额 -->
    <section>
      <h3 class="mb-1 text-sm font-semibold text-slate-800">限额</h3>
      <div class="grid gap-3 sm:grid-cols-2">
        <el-form-item label="每秒请求上限（retrieve_qps）">
          <div class="flex items-center gap-2">
            <el-input-number v-model="form.retrieve_qps" :min="1" :step="1" controls-position="right" />
            <span class="text-xs text-slate-500">次/秒</span>
          </div>
          <p v-if="qpsHint !== null" class="mt-1 text-xs text-slate-400">{{ qpsHint }}</p>
        </el-form-item>
        <el-form-item label="每日请求上限（retrieve_daily）">
          <div class="flex items-center gap-2">
            <el-input-number
              v-model="form.retrieve_daily"
              :min="1"
              :step="100"
              controls-position="right"
            />
            <span class="text-xs text-slate-500">次/天</span>
          </div>
          <p v-if="dailyHint !== null" class="mt-1 text-xs text-slate-400">{{ dailyHint }}</p>
        </el-form-item>
      </div>
    </section>

    <!-- 第四组：默认档位 -->
    <section>
      <h3 class="mb-1 text-sm font-semibold text-slate-800">默认档位</h3>
      <el-form-item label="档位（profile）">
        <el-select v-model="form.profile" class="w-full">
          <el-option
            v-for="profile in KEY_PROFILES"
            :key="profile"
            :value="profile"
            :label="KEY_PROFILE_LABELS[profile]"
            :disabled="blockedProfiles.has(profile)"
          >
            <span :title="blockedProfiles.has(profile) ? blockedProfileReason : undefined">
              {{ KEY_PROFILE_LABELS[profile] }}
            </span>
            <span v-if="blockedProfiles.has(profile)" class="ml-2 text-xs text-slate-400">
              {{ blockedProfileReason }}
            </span>
          </el-option>
        </el-select>
        <p class="mt-1 text-xs text-slate-500">
          档位决定「能用到多好」；它所需的能力必须已在上面的开关里打开。
          <span v-if="allowedProfiles.length > 0">
            当前套餐允许：{{ allowedProfiles.join('、') }}。
          </span>
          <span v-else>（还没读到套餐白名单：档位全部置灰）</span>
        </p>
      </el-form-item>
    </section>

    <!-- 第五组：结果形状 -->
    <section>
      <h3 class="mb-1 text-sm font-semibold text-slate-800">结果形状</h3>
      <div class="grid gap-2 sm:grid-cols-2">
        <el-checkbox v-model="form.include_parent">include_parent（折叠成整节）</el-checkbox>
        <el-checkbox v-model="form.expand_table_rows">
          expand_table_rows（表格行折回整表）
        </el-checkbox>
      </div>
      <p class="mt-1 text-xs text-slate-500">
        这两项只改返回的上下文形状，不影响召回与排序。
      </p>
    </section>

    <!-- custom 专属：只在 profile === "custom" 时渲染；切走即卸载 -->
    <section v-if="visibleCustomOptions(form.profile)">
      <h3 class="mb-1 text-sm font-semibold text-slate-800">
        custom 手动参数
        <span class="ml-2 text-xs font-normal text-slate-500">仅 custom 档生效</span>
      </h3>
      <div class="grid gap-3 sm:grid-cols-2">
        <el-form-item label="检索方式（custom_options.mode）">
          <el-select v-model="form.custom_options.mode" class="w-full">
            <el-option
              v-for="mode in CUSTOM_OPTION_MODES"
              :key="mode"
              :label="mode"
              :value="mode"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="返回条数（custom_options.top_k）">
          <el-input-number
            v-model="form.custom_options.top_k"
            :min="CUSTOM_TOP_K_MIN"
            :max="CUSTOM_TOP_K_MAX"
            controls-position="right"
          />
        </el-form-item>
        <el-checkbox v-model="form.custom_options.rerank">rerank（重排）</el-checkbox>
        <el-checkbox v-model="form.custom_options.rewrite">rewrite（查询改写）</el-checkbox>
      </div>
      <p class="mt-1 text-xs text-slate-500">
        命名档位是完整替换：切到 speed / balanced / quality 后这批参数不生效，但也不会被删。
      </p>
    </section>
  </el-form>
</template>
