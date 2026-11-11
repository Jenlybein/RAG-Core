// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

/**
 * 面板的三条硬规矩，逐条钉住：
 *
 * 1. **写操作不出现「试一下」**——这是安全底线：真实 Key + 真实租户数据，误点就真改了；
 * 2. 只读的点下去会**真的调对应服务**，并把原始响应显示出来；
 * 3. 需要库的那条没勾库时**不给按钮**（「能点但必然失败」比没有按钮更糟）。
 *
 * 这类「模板接线」的错误（按钮绑了个 null）在仓库里真实发生过一次（上传文件夹），
 * 而它当时没有任何测试能发现。
 */

const { fetchKbs, fetchMe, fetchDocs } = vi.hoisted(() => ({
  fetchKbs: vi.fn(),
  fetchMe: vi.fn(),
  fetchDocs: vi.fn(),
}))

vi.mock('../../services/scopeService', () => ({
  fetchMyKnowledgeBases: fetchKbs,
  fetchDataPlaneAuthMe: fetchMe,
  fetchDocumentList: fetchDocs,
}))

const { default: DataPlanePanel } = await import('../DataPlanePanel.vue')

const STUBS = {
  'el-button': { template: '<button type="button"><slot /></button>' },
}

function mountPanel(props: { kbId: string | null; hasKey: boolean }) {
  return mount(DataPlanePanel, {
    props,
    global: { stubs: STUBS },
  })
}

/** 表格行：第 0 行是表头。 */
function rows(wrapper: ReturnType<typeof mountPanel>) {
  return wrapper.findAll('tbody tr')
}

function rowOf(wrapper: ReturnType<typeof mountPanel>, path: string) {
  return rows(wrapper).find((row) => row.text().includes(path))
}

describe('DataPlanePanel：清单与试跑', () => {
  it('列出全部 9 条数据面接口', () => {
    expect(rows(mountPanel({ kbId: 'kb-a', hasKey: true }))).toHaveLength(9)
  })

  it('写操作只列出，不给「试一下」', () => {
    const wrapper = mountPanel({ kbId: 'kb-a', hasKey: true })
    const upload = rowOf(wrapper, '/documents/upload')

    expect(upload).toBeDefined()
    expect(upload!.text()).toContain('写：不代跑')
    // 整行都不该出现「试一下」这三个字。
    expect(upload!.text()).not.toContain('试一下')
  })

  it('只读的那条点下去会真的调服务，并显示原始响应', async () => {
    fetchMe.mockResolvedValue({ tenant: { tenant_id: 'tenant_beta' } })
    const wrapper = mountPanel({ kbId: 'kb-a', hasKey: true })

    const row = rowOf(wrapper, '/auth/me')
    const button = row!.findAll('button').find((item) => item.text().includes('试一下'))
    expect(button, '/auth/me 应当有试跑按钮').toBeDefined()

    await button!.trigger('click')
    await flushPromises()

    expect(fetchMe).toHaveBeenCalledTimes(1)
    // 原样 JSON：tenant_beta 要能在页面上看到。
    expect(wrapper.text()).toContain('tenant_beta')
  })

  it('没选 Key 时整块提示，且一个按钮都不给', () => {
    const wrapper = mountPanel({ kbId: null, hasKey: false })

    expect(wrapper.text()).toContain('先选一把 Key')
    expect(wrapper.findAll('button')).toHaveLength(0)
  })

  it('需要库的那条：没勾库时不给按钮，并说出它需要什么', () => {
    const wrapper = mountPanel({ kbId: null, hasKey: true })
    const documents = rowOf(wrapper, '/documents')

    expect(documents).toBeDefined()
    expect(documents!.text()).toContain('用当前勾选的第一个库')
    expect(documents!.findAll('button')).toHaveLength(0)
  })

  it('勾了库之后，需要库的那条就能试，且把 kb_id 传下去', async () => {
    fetchDocs.mockResolvedValue({ documents: [] })
    const wrapper = mountPanel({ kbId: 'kb-42', hasKey: true })

    const row = rowOf(wrapper, '/documents')
    const button = row!.findAll('button').find((item) => item.text().includes('试一下'))
    await button!.trigger('click')
    await flushPromises()

    expect(fetchDocs).toHaveBeenCalledWith('kb-42')
  })

  it('失败时显示错误文案，而不是静默', async () => {
    fetchMe.mockRejectedValue(new Error('boom'))
    const wrapper = mountPanel({ kbId: 'kb-a', hasKey: true })

    const row = rowOf(wrapper, '/auth/me')
    await row!
      .findAll('button')
      .find((item) => item.text().includes('试一下'))!
      .trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('boom')
  })
})
