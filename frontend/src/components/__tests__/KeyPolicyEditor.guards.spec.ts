// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { nextTick, ref } from 'vue'

import KeyPolicyEditor from '../KeyPolicyEditor.vue'
import type { ConsolePlanSnapshot } from '../../types/console'
import type { PolicyForm } from '../../types/key'

/**
 * 「能力位开着、模型额度却是 0」必须在**勾选的那一屏**就说清。
 *
 * 真实故障（2026-11-27）：用户在能力开关里勾上「查询改写」，高级设置里的模型额度留着默认
 * 0，一用「改写后检索」就收到 `20005 操作太频繁：稍等一会儿再试`。那句话让人一直等下去——
 * 真正的原因是这本账没开，而它藏在折叠起来的「高级设置」里，不展开就看不到。
 *
 * 这里钉两件事：
 * 1. 勾选之后界面出现警告（而不是等用户展开折叠面板才看到）；
 * 2. 勾选那一刻按**套餐上限**把额度补上，并且这一步是可见的（输入框那两格真的变了）。
 *
 * 用 Element Plus 的桩：这一条验的是「勾选 → 警告 / 补齐」的接线，不是弹窗长什么样。
 */

const PLAN: ConsolePlanSnapshot = {
  code: 'pro',
  name: 'pro',
  retrieve_allowed: true,
  rewrite_allowed: true,
  decompose_allowed: true,
  research_allowed: true,
  rerank_allowed: true,
  retrieve_qps: 10,
  retrieve_daily: 25000,
  rerank_qps: 15,
  rerank_daily: 5000,
  llm_qps: 5,
  llm_daily: 1500,
  max_top_k: 50,
  max_kb: 10,
  max_kb_per_retrieve: 5,
  max_documents_per_kb: 1000,
  max_tokens_per_kb: 2_000_000,
  chunk_daily_tokens: 5_000_000,
}

function form(): PolicyForm {
  return {
    allowed_kb_ids: ['kb-1'],
    retrieve_allowed: true,
    rewrite_allowed: false,
    decompose_allowed: false,
    research_allowed: false,
    rerank_allowed: false,
    retrieve_qps: 10,
    retrieve_daily: 25000,
    rerank_qps: 0,
    rerank_daily: 0,
    llm_qps: 0,
    llm_daily: 0,
    retrieval_mode: 'vector',
    retrieval_top_k: 5,
    retrieval_rerank: false,
    include_parent: true,
    expand_table_rows: true,
    max_unit_chars: 2000,
  }
}

const stubs = {
  // 具名桩：下面要用 `findAllComponents({ name: 'ElCheckbox' })` 拿到组件实例再 `$emit('change')`。
  // 用 DOM 的 `trigger('change')` 是**不会**触发 `@change` 的——那是组件事件，不是原生事件，
  // 这个区别正是第一版这条用例假红的原因。
  'el-checkbox': {
    name: 'ElCheckbox',
    props: ['modelValue'],
    emits: ['update:modelValue', 'change'],
    template: '<label class="cb"><slot /></label>',
  },
  'el-tooltip': { template: '<span><slot /></span>' },
  'el-collapse': { template: '<div><slot /></div>' },
  'el-collapse-item': { template: '<div><slot /><slot name="title" /></div>' },
  'el-input-number': { template: '<input />' },
  'el-select': { template: '<select><slot /></select>' },
  'el-option': { template: '<option />' },
  'el-switch': { template: '<span />' },
  'el-button': { template: '<button><slot /></button>' },
  'el-radio-group': { template: '<div><slot /></div>' },
  'el-radio': { template: '<label><slot /></label>' },
}

function mountEditor(model: PolicyForm) {
  const form_ = ref<PolicyForm>(model)
  const wrapper = mount(KeyPolicyEditor, {
    props: {
      kbOptions: [{ kbId: 'kb-1', name: '库一' }],
      plan: PLAN,
      modelValue: form_.value,
      'onUpdate:modelValue': (value: PolicyForm) => {
        form_.value = value
      },
    } as never,
    global: { stubs },
  })
  return { wrapper, form: form_ }
}

/**
 * 模拟「用户勾上一个能力位」：v-model 已经写回父级，然后触发组件里那次 change。
 *
 * 为什么先 `setProps` 再触发 change：组件读的是 `props.modelValue`（`defineModel` 在
 * 没有本地默认值时就是这个来源），只改我手里的 ref 不动 prop，组件那边看到的还是旧值。
 * 而这个桩没有绑定 `form[field]`，所以能验的正是「change → 补齐 / 警告」这条接线。
 */
async function toggle(
  wrapper: { setProps: (props: Record<string, unknown>) => Promise<void> },
  formRef: { value: PolicyForm },
  field: keyof PolicyForm,
): Promise<void> {
  formRef.value = { ...formRef.value, [field]: true } as PolicyForm
  await wrapper.setProps({ modelValue: formRef.value })
}

describe('KeyPolicyEditor 模型额度守卫', () => {
  it('勾上改写时按套餐上限把模型额度补齐，并给出警告说明', async () => {
    const { wrapper, form: formRef } = mountEditor(form())

    await toggle(wrapper, formRef, 'rewrite_allowed')
    const checkbox = wrapper.findAllComponents({ name: 'ElCheckbox' })[1]
    checkbox.vm.$emit('change')
    await nextTick()

    // 补齐用的是套餐上限，不是某个魔数。
    expect(formRef.value.llm_qps).toBe(5)
    expect(formRef.value.llm_daily).toBe(1500)

    // 真实环境里父级会把新的 form 再传回来（v-model 的双向），重现那一步再看渲染。
    await wrapper.setProps({ modelValue: formRef.value })

    // 补齐之后不再是「注定失败」的配置，所以警告应当消失。
    expect(wrapper.text()).not.toContain('这本账没开')
  })

  it('两格都是 0 且能力位开着时，勾选的那一屏就出现警告', async () => {
    const { wrapper, form: formRef } = mountEditor(form())
    // 套餐本身也给不出额度 → 没有可补的值，警告必须出现（这时只能靠改配置或关位）。
    await wrapper.setProps({ plan: { ...PLAN, llm_qps: 0, llm_daily: 0 } } as never)

    await toggle(wrapper, formRef, 'rewrite_allowed')
    await nextTick()

    expect(wrapper.text()).toContain('这本账没开')
    expect(wrapper.text()).toContain('不是「调用太快」')
  })

  it('能力位全关时不出现警告（两本账为 0 是合法的「这套用它吗」）', () => {
    const { wrapper } = mountEditor(form())
    expect(wrapper.text()).not.toContain('这本账没开')
  })
})
