<script setup lang="ts">
/**
 * 密钥配置编辑器：五组字段 + 只在 `custom` 档出现的手工参数。
 *
 * **职责边界**：它只「渲染表单并改父级传进来的那个 form 对象」，不发请求、不做自洽校验。
 * 校验与保存由 `KeysPage.vue` 调 `keyService` 完成，判定（哪些档位被套餐挡住、哪些字段
 * 算改动）由 `utils/keyPolicy.ts` 的纯函数给出——组件里再写一份就会出现「界面允许保存、
 * 后端 20015 拒绝」的分叉。
 *
 * **为什么这么排布**（这是本次改版的重点）：
 *
 * 一屏里同时摆 11 个字段 + 每段都配一段解释，读起来像一张考卷。真实的决策顺序是
 * 「选档位 → 圈定数据范围 → 剩下的用默认值」，所以：
 *
 * 1. **档位放最前面**，做成四张可选卡片（一眼看到四档的差别与哪几档被套餐挡住），
 *    而不是一个下拉——下拉把选项藏起来，用户得点两次才能比较。
 * 2. **数据范围**紧跟其后，它是第二重要的决定（Key 能查什么）。
 * 3. **能力开关**保持可见但压成两列，并合并成一句提示：这一段必须看得见，因为命名档位
 *    要求它所需的能力全部打开，否则保存会被后端拒。
 * 4. **限额与结果形状收进「高级设置」折叠**（默认收起，标题上带实时摘要）。它们是
 *    「用默认值就行」的那一类，展开后仍在原位，不影响可发现性。
 *
 * 文案上删掉了 `retrieve_qps` / `allowed_kb_ids` 这类后端字段名：界面是给人看的，
 * 字段名在接口文档里；把它们留在标签里只会让每一行都变长。真正需要解释的取舍
 * 用一句话说清，不再堆叠三段说明。
 */
import { computed } from 'vue'

import {
  CUSTOM_OPTION_MODES,
  CUSTOM_TOP_K_MAX,
  CUSTOM_TOP_K_MIN,
  KEY_PROFILES,
  type KbOption,
  type KeyProfile,
  type PolicyForm,
} from '../types/key'
import type { ProfilePresetData } from '../types/profilePreset'
import { describePreset } from '../services/consoleProfiles'
import { disabledProfiles, visibleCustomOptions } from '../utils/keyPolicy'

/** 父级持有的表单对象：子组件直接改它，保存时读的是同一份状态。 */
const form = defineModel<PolicyForm>({ required: true })

const props = defineProps<{
  /** `GET /console/me` 里套餐的 `allowed_profiles`：只从这一份白名单里放行。 */
  allowedProfiles: string[]
  /** 控制面树里的库：`allowed_kb_ids` 的候选来源。 */
  kbOptions: KbOption[]
  /**
   * 四个档位的当前参数（`me.profile_presets`）。**这是「档位能干什么」的唯一数据源**：
   * 后台能改它，所以前端不能写死「quality = 含重排」——那样后台关掉重排之后界面还在
   * 承诺它。空数组时退回只显示档位名（旧后端不返回这个字段）。
   */
  presets?: ProfilePresetData[]
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

/** 卡片上的两行：短名 + 一句差别。`custom` 的说明指向下面那组手动参数。 */
const PROFILE_CARDS: Record<KeyProfile, { title: string; hint: string }> = {
  speed: { title: 'speed', hint: '只走向量检索，最快最省' },
  balanced: { title: 'balanced', hint: '向量 + 关键词，两路都召回' },
  quality: { title: 'quality', hint: '混合检索 + 重排 + 改写' },
  custom: { title: 'custom', hint: '自己填检索方式与条数' },
}

/** 折叠面板的标题摘要：收起时也要能看出「现在是多少」，否则等于藏了信息。 */
const advancedSummary = computed(
  () => `${form.value.retrieve_qps} 次/秒 · ${form.value.retrieve_daily} 次/天`,
)

const planCapabilityText = computed(() => {
  const plan = props.planHint
  if (plan === null) return ''
  const allowed: string[] = []
  if (plan.researchAllowed) allowed.push('research')
  if (plan.hybridAllowed) allowed.push('hybrid')
  if (plan.rerankAllowed) allowed.push('rerank')
  if (plan.queryRewriteAllowed) allowed.push('query_rewrite')
  return allowed.length === 0 ? '' : allowed.join(' / ')
})

const qpsCap = computed(() => props.planHint?.retrieveQps ?? null)
const dailyCap = computed(() => props.planHint?.retrieveDaily ?? null)

/** 档位名 → 参数。空数组或漏一档时返回 `undefined`，由模板决定怎么退化。 */
function presetOf(profile: string): ProfilePresetData | undefined {
  const list = props.presets ?? []
  return list.find((item) => item.profile === profile)
}

/**
 * 卡片上那一行小字：**档位实际会做什么**（来自后台可改的参数），而不是写死的文案。
 * 拿不到参数时退回原来的静态说明——「不知道」不该显示成「什么都没配」。
 */
function cardHint(profile: KeyProfile): string {
  const preset = presetOf(profile)
  if (preset === undefined) return PROFILE_CARDS[profile].hint
  return describePreset(preset)
}

/**
 * 悬浮提示的正文：参数 + 一句「这是意图不是生效值」。
 * 为什么补这句：套餐允许 rerank、档位想要 rerank，但本机没装重排时它**不会跑**，
 * 而用户看到「结果重排」会以为一定会重排。
 */
function presetTooltip(profile: KeyProfile): string {
  const preset = presetOf(profile)
  if (preset === undefined) {
    return `${PROFILE_CARDS[profile].title}：${PROFILE_CARDS[profile].hint}（还没读到参数）`
  }
  return `${preset.profile} 档位的参数：${describePreset(preset)}。重排与改写是「想要」，实际还要本机装了才会跑。`
}

/** 只有一个库时不必让用户展开下拉：省掉一次没有信息量的点击。 */
const onlyKbId = computed(() => (props.kbOptions.length === 1 ? props.kbOptions[0].kb_id : null))
</script>

<template>
  <el-form label-position="top" class="space-y-6">
    <!-- ① 档位：主决策，做成可选卡片 -->
    <section>
      <div class="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h3 class="section-title">档位</h3>
        <span v-if="allowedProfiles.length > 0" class="text-xs text-muted">
          套餐允许 {{ allowedProfiles.join(' / ') }}
        </span>
      </div>

      <div class="grid gap-2 sm:grid-cols-2">
        <!--
          档位卡片。悬浮（或键盘聚焦）时给一条提示，把这一档的参数说出来——
          「能干什么」不是名字能回答的问题，而展开四张卡片的细节会把弹窗撑爆。
          按钮本身仍然可点：提示是补充信息，不是一层拦截。
        -->
        <el-tooltip
          v-for="profile in KEY_PROFILES"
          :key="profile"
          :content="presetTooltip(profile)"
          placement="top"
          :show-after="300"
          :hide-after="0"
          popper-class="max-w-[320px]"
        >
          <button
            type="button"
            class="w-full rounded-control border px-3.5 py-2.5 text-left transition-colors duration-150"
            :class="[
              form.profile === profile
                ? 'border-accent bg-accent-soft'
                : 'border-line bg-surface hover:border-line-strong',
              blockedProfiles.has(profile) ? 'cursor-not-allowed opacity-45' : '',
            ]"
            :disabled="blockedProfiles.has(profile)"
            @click="form.profile = profile"
          >
            <span class="flex items-center justify-between gap-2">
              <span class="text-[13px] font-semibold text-ink">{{
                PROFILE_CARDS[profile].title
              }}</span>
              <!-- 单选的可视化：一个圆点，选中时内圈填实 -->
              <span
                class="grid h-3.5 w-3.5 shrink-0 place-items-center rounded-full border"
                :class="form.profile === profile ? 'border-accent' : 'border-line-strong'"
              >
                <span v-if="form.profile === profile" class="h-1.5 w-1.5 rounded-full bg-accent" />
              </span>
            </span>
            <span class="mt-0.5 block text-xs leading-snug text-muted">
              {{ blockedProfiles.has(profile) ? '当前套餐不支持' : cardHint(profile) }}
            </span>
          </button>
        </el-tooltip>
      </div>

      <p class="mt-2 text-xs leading-relaxed text-muted">
        档位决定「能用到多好」；命名档位要求它所需的能力已在下方的开关里打开，否则保存会被拒。
      </p>
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

    <!-- ③ 能力开关：必须可见（命名档位依赖它），压成两列 -->
    <section>
      <h3 class="section-title mb-0.5">能力开关</h3>
      <p class="mb-2 text-xs leading-relaxed text-muted">
        只能在套餐上限内分配。<template v-if="planCapabilityText !== ''">
          本套餐可用：{{ planCapabilityText }}。
        </template>
      </p>
      <div class="grid gap-x-4 gap-y-1.5 sm:grid-cols-2">
        <el-checkbox v-model="form.research_allowed">research · 研究型检索</el-checkbox>
        <el-checkbox v-model="form.hybrid_allowed">hybrid · 向量 + 关键词</el-checkbox>
        <el-checkbox v-model="form.rerank_allowed">rerank · 结果重排</el-checkbox>
        <el-checkbox v-model="form.query_rewrite_allowed">query_rewrite · 查询改写</el-checkbox>
      </div>
    </section>

    <!-- ④ 高级：限额 / 结果形状 / custom 手动参数，默认收起 -->
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
            <h4 class="mb-2 text-[12px] font-semibold text-slate-700">限额</h4>
            <div class="grid gap-3 sm:grid-cols-2">
              <div>
                <label class="mb-1 block text-xs text-muted">每秒请求上限</label>
                <div class="flex items-center gap-2">
                  <el-input-number v-model="form.retrieve_qps" :min="1" :step="1" controls-position="right" />
                  <span class="text-xs text-muted">次/秒</span>
                </div>
                <p v-if="qpsCap !== null" class="mt-1 text-xs text-muted">套餐上限 {{ qpsCap }}</p>
              </div>
              <div>
                <label class="mb-1 block text-xs text-muted">每日请求上限</label>
                <div class="flex items-center gap-2">
                  <el-input-number v-model="form.retrieve_daily" :min="1" :step="100" controls-position="right" />
                  <span class="text-xs text-muted">次/天</span>
                </div>
                <p v-if="dailyCap !== null" class="mt-1 text-xs text-muted">套餐上限 {{ dailyCap }}</p>
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

          <!-- custom 专属：只在 profile === "custom" 时渲染；切走即卸载。
               `v-show` 只是隐藏，输入框还挂在表单上，切回命名档位后那批失效的值仍会被
               buildPolicyPatch 当作改动发出去——`v-if` 在切走时直接卸载，两道锁。 -->
          <div v-if="visibleCustomOptions(form.profile)">
            <h4 class="mb-1 text-[12px] font-semibold text-slate-700">
              custom 手动参数
              <span class="ml-1 font-normal text-muted">仅 custom 档生效</span>
            </h4>
            <div class="grid gap-3 sm:grid-cols-2">
              <div>
                <label class="mb-1 block text-xs text-muted">检索方式</label>
                <el-select v-model="form.custom_options.mode" class="w-full">
                  <el-option v-for="mode in CUSTOM_OPTION_MODES" :key="mode" :label="mode" :value="mode" />
                </el-select>
              </div>
              <div>
                <label class="mb-1 block text-xs text-muted">返回条数</label>
                <el-input-number
                  v-model="form.custom_options.top_k"
                  :min="CUSTOM_TOP_K_MIN"
                  :max="CUSTOM_TOP_K_MAX"
                  controls-position="right"
                />
              </div>
              <el-checkbox v-model="form.custom_options.rerank">rerank · 重排</el-checkbox>
              <el-checkbox v-model="form.custom_options.rewrite">rewrite · 查询改写</el-checkbox>
            </div>
            <p class="mt-1 text-xs leading-relaxed text-muted">
              命名档位是完整替换：切回 speed / balanced / quality 后这批参数不生效，但也不会被删。
            </p>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>
  </el-form>
</template>
