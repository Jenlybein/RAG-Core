<script setup lang="ts">
/**
 * 密钥配置编辑器：五组字段（范围 / 五个授权位 / 三本账额度 / 三项直参数 / 形状）。
 *
 * **职责边界**：它只「渲染表单并改父级传进来的那个 form 对象」，不发请求、不做自洽校验。
 * 校验与保存由 `KeysPage.vue` 调 `keyService` 完成，判定（哪些字段算改动、预设怎么填）
 * 由 `utils/keyPolicy.ts` 的纯函数给出——组件里再写一份就会出现「界面允许保存、
 * 后端 20015 拒绝」的分叉。
 *
 * **第 13 步起没有档位**（ADR-0032）：用多好由三项直参数表达，speed / balanced /
 * quality 只是「一键填好这三项」的预设按钮，不承诺链路、不参与判定。
 *
 * 排布按真实决策顺序：① 检索参数（主决策，预设按钮 + 三个可直接改的输入）→
 * ② 数据范围（Key 能查什么）→ ③ 能力开关（必须可见：参数想要重排，还得授权位允许）→
 * ④ 限额与结果形状收进「高级设置」折叠（默认收起，标题带实时摘要）。
 */
import { computed } from 'vue'

import type { ConsolePlanSnapshot } from '../types/console'
import {
  PARAM_PRESETS,
  POLICY_CAPABILITY_FIELDS,
  RETRIEVAL_MODES,
  TOP_K_MAX,
  TOP_K_MIN,
  type KbOption,
  type PolicyForm,
} from '../types/key'
import { applyParamPreset } from '../utils/keyPolicy'

/** 父级持有的表单对象：子组件直接改它，保存时读的是同一份状态。 */
const form = defineModel<PolicyForm>({ required: true })

const props = defineProps<{
  /** 控制面树里的库：`allowed_kb_ids` 的候选来源。 */
  kbOptions: KbOption[]
  /** `GET /console/me` 里的套餐快照：授权位与额度的上限；还没读到时为 `null`。 */
  plan: ConsolePlanSnapshot | null
}>()

/** 五个授权位的界面说法；字段名与套餐快照同名，置灰判定直接读 `plan[field]`。 */
const CAPABILITY_LABELS: Record<(typeof POLICY_CAPABILITY_FIELDS)[number], string> = {
  retrieve_allowed: 'retrieve · 检索',
  rewrite_allowed: 'rewrite · 查询改写',
  decompose_allowed: 'decompose · 查询分解',
  research_allowed: 'research · 研究模式',
  rerank_allowed: 'rerank · 结果重排',
}

/** 套餐没给的授权位要置灰：界面放行而保存被 20015 拒，是最糟的分叉。 */
function capabilityDisabled(field: (typeof POLICY_CAPABILITY_FIELDS)[number]): boolean {
  return props.plan !== null && !props.plan[field]
}

const planCapabilityText = computed(() => {
  const plan = props.plan
  if (plan === null) return ''
  const allowed = POLICY_CAPABILITY_FIELDS.filter((field) => plan[field]).map(
    (field) => CAPABILITY_LABELS[field].split(' · ')[0],
  )
  return allowed.length === 0 ? '' : allowed.join(' / ')
})

/** 套餐的 `max_top_k` 封顶；0 表示不额外封顶，退回平台硬上限（与后端裁剪同义）。 */
const topKMax = computed(() => {
  const cap = props.plan?.max_top_k ?? 0
  return cap > 0 ? Math.min(TOP_K_MAX, cap) : TOP_K_MAX
})

/**
 * 预设是**动作**，不是**状态**：它们不出现在表单里（后端也没有这一列），只在用户
 * 主动选一下时把三项参数填好。所以界面用「按钮 + 下拉」而不是一排常驻的选中态按钮——
 * 常驻按钮会被读成「当前选的是 balanced 档」，而档位这个概念第 13 步已经不存在了。
 */
function fillPresetByName(name: string): void {
  const preset = PARAM_PRESETS.find((item) => item.name === name)
  if (preset === undefined) return
  form.value = applyParamPreset(form.value, preset)
}

/** 折叠面板的标题摘要：收起时也要能看出「现在是多少」，否则等于藏了信息。 */
const advancedSummary = computed(
  () => `${form.value.retrieve_qps} 次/秒 · ${form.value.retrieve_daily} 次/天`,
)

/** 六个额度输入框的配置：标签、单位、最小值、对应的套餐上限字段。 */
const QUOTA_INPUTS: readonly {
  key: 'retrieve_qps' | 'retrieve_daily' | 'rerank_qps' | 'rerank_daily' | 'llm_qps' | 'llm_daily'
  label: string
  unit: string
  min: number
  step: number
}[] = [
  { key: 'retrieve_qps', label: '检索每秒上限', unit: '次/秒', min: 1, step: 1 },
  { key: 'retrieve_daily', label: '检索每日上限', unit: '次/天', min: 1, step: 100 },
  { key: 'rerank_qps', label: '重排每秒上限', unit: '次/秒', min: 0, step: 1 },
  { key: 'rerank_daily', label: '重排每日上限', unit: '次/天', min: 0, step: 100 },
  { key: 'llm_qps', label: '模型每秒上限', unit: '次/秒', min: 0, step: 1 },
  { key: 'llm_daily', label: '模型每日上限', unit: '次/天', min: 0, step: 100 },
]

/** 只有一个库时不必让用户展开下拉：省掉一次没有信息量的点击。 */
const onlyKbId = computed(() => (props.kbOptions.length === 1 ? props.kbOptions[0].kb_id : null))
</script>

<template>
  <el-form label-position="top" class="space-y-6">
    <!-- ① 检索参数：主决策。预设按钮一键填三项，每一项仍可单独改 -->
    <section>
      <h3 class="section-title mb-2">检索参数</h3>
      <div class="mb-3 flex flex-wrap items-center gap-2">
        <el-dropdown trigger="click" @command="fillPresetByName">
          <el-button size="small">使用预设配置</el-button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item
                v-for="preset in PARAM_PRESETS"
                :key="preset.name"
                :command="preset.name"
              >
                <span class="font-medium text-ink">{{ preset.name }}</span>
                <span class="ml-2 text-xs text-muted">{{ preset.hint }}</span>
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <span class="text-xs text-muted">
          选一个会覆盖下面三项，之后仍可单独改。预设只是填空，不是档位。
        </span>
      </div>

      <div class="grid gap-3 sm:grid-cols-3">
        <div>
          <label class="mb-1 block text-xs text-muted">检索方式</label>
          <el-select v-model="form.retrieval_mode" class="w-full">
            <el-option v-for="mode in RETRIEVAL_MODES" :key="mode" :label="mode" :value="mode" />
          </el-select>
        </div>
        <div>
          <label class="mb-1 block text-xs text-muted">返回条数（top_k）</label>
          <el-input-number
            v-model="form.retrieval_top_k"
            :min="TOP_K_MIN"
            :max="topKMax"
            controls-position="right"
            class="!w-full"
          />
          <p v-if="plan !== null && plan.max_top_k > 0" class="mt-1 text-xs text-muted">
            套餐封顶 {{ plan.max_top_k }}
          </p>
        </div>
        <div>
          <label class="mb-1 block text-xs text-muted">结果重排</label>
          <el-switch v-model="form.retrieval_rerank" />
          <p class="mt-1 text-xs text-muted">还需下方 rerank 授权位打开才生效。</p>
        </div>
      </div>
    </section>

    <!-- ② 数据范围：第二重要的决定 -->
    <section>
      <h3 class="section-title mb-2">数据范围</h3>
      <el-select
        v-model="form.allowed_kb_ids"
        multiple
        filterable
        collapse-tags
        collapse-tags-tooltip
        class="w-full"
        placeholder="未圈定任何库（不是全部）"
      >
        <el-option
          v-for="kb in kbOptions"
          :key="kb.kb_id"
          :label="kb.name"
          :value="kb.kb_id"
        />
      </el-select>
      <p class="mt-1.5 text-xs leading-relaxed text-muted">
        只有圈进来的库能被这把 Key 检索；留空表示「什么都没圈定」，不是「全部」。
        <template v-if="onlyKbId !== null">
          <el-button link type="primary" size="small" class="!ml-1" @click="form.allowed_kb_ids = [onlyKbId]">
            圈定唯一那个库
          </el-button>
        </template>
      </p>
    </section>

    <!-- ③ 能力开关：五个授权位，套餐没给的置灰 -->
    <section>
      <h3 class="section-title mb-0.5">能力开关</h3>
      <p class="mb-2 text-xs leading-relaxed text-muted">
        只能在套餐授权范围内使用。<template v-if="planCapabilityText !== ''">
          本套餐可用：{{ planCapabilityText }}。
        </template>
      </p>
      <div class="grid gap-x-4 gap-y-1.5 sm:grid-cols-2">
        <el-checkbox
          v-for="field in POLICY_CAPABILITY_FIELDS"
          :key="field"
          v-model="form[field]"
          :disabled="capabilityDisabled(field)"
        >
          {{ CAPABILITY_LABELS[field] }}
          <span v-if="capabilityDisabled(field)" class="text-xs text-muted">（当前套餐不支持）</span>
        </el-checkbox>
      </div>
    </section>

    <!-- ④ 高级：三本账额度 / 结果形状，默认收起 -->
    <el-collapse>
      <el-collapse-item name="advanced">
        <template #title>
          <span class="flex flex-wrap items-baseline gap-2">
            <span class="section-title">高级设置</span>
            <span class="text-xs text-muted">{{ advancedSummary }}</span>
          </span>
        </template>

        <div class="space-y-5 pt-1">
          <div>
            <h4 class="mb-1 text-[12px] font-semibold text-slate-700">限额（三本账）</h4>
            <p class="mb-2 text-xs leading-relaxed text-muted">
              检索、重排、模型各记各的账；加工能力的额度填 0 表示「不给这项额度」。
            </p>
            <div class="grid gap-3 sm:grid-cols-2">
              <div v-for="input in QUOTA_INPUTS" :key="input.key">
                <label class="mb-1 block text-xs text-muted">{{ input.label }}</label>
                <div class="flex items-center gap-2">
                  <el-input-number
                    v-model="form[input.key]"
                    :min="input.min"
                    :step="input.step"
                    controls-position="right"
                  />
                  <span class="text-xs text-muted">{{ input.unit }}</span>
                </div>
                <p v-if="plan !== null" class="mt-1 text-xs text-muted">
                  套餐上限 {{ plan[input.key] }}
                </p>
              </div>
            </div>
          </div>

          <div>
            <h4 class="mb-1 text-[12px] font-semibold text-slate-700">结果形状</h4>
            <p class="mb-2 text-xs leading-relaxed text-muted">
              只改返回的上下文形状，不影响召回与排序。
            </p>
            <!--
              这里显示的是**概念名**（small-to-big / rows-to-table），不是请求体字段名。
              字段仍然是 `include_parent` / `expand_table_rows`：后端那两个 schema 是
              `extra="forbid"`，把字段名一起改掉会立刻 20002。概念名是给人看的口径，
              字段名是契约，两者不必同名——但改契约要单独做一次迁移，不混在这次改文案里。
            -->
            <div class="grid gap-x-4 gap-y-1.5 sm:grid-cols-2">
              <el-checkbox v-model="form.include_parent">
                small-to-big · 检索小块返回大块
              </el-checkbox>
              <el-checkbox v-model="form.expand_table_rows">
                rows-to-table · 检索表行返回整表
              </el-checkbox>
            </div>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>
  </el-form>
</template>