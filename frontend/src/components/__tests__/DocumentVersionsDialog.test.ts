// @vitest-environment jsdom
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * 版本弹窗的三条硬规矩（票 #177）：
 *
 * 1. **回滚前必须确认，而且确认框要说「会影响所有调用方」**——这是票面验收项：面板上
 *    一次点击改的是整个租户检索到的内容，不是当前这一页的显示；
 * 2. 列表带稿号 / 状态 / 字符数 / 时间；**当前稿不给回滚入口**（后端会回 20032）；
 * 3. 确认之后只调**一个**接口（`/console/documents/{id}/versions/{v}/rollback`），
 *    并把稿号原样带过去——参数错位会让「回滚 v1」变成「回滚 v3」，而这种错误在
 *    界面上看起来完全正常。
 *
 * 只桩掉提示条与 Element Plus 的按钮 / 弹窗外壳：要验的是接线与文案，不是它们长什么样。
 */

const mocks = vi.hoisted(() => ({
  fetchVersions: vi.fn(),
  rollback: vi.fn(),
}))

vi.mock('../../services/consoleKb', () => ({
  fetchDocumentVersionsConsole: mocks.fetchVersions,
  rollbackDocumentVersionConsole: mocks.rollback,
}))

vi.mock('element-plus', () => ({
  ElMessage: { success: vi.fn(), error: vi.fn() },
}))

const { default: DocumentVersionsDialog } = await import('../DocumentVersionsDialog.vue')

const STUBS = {
  // 外壳要把 `title` 渲出来：确认框的标题（「回滚到第 N 稿」）本身就是要验的文案之一。
  'el-dialog': {
    props: ['title'],
    template:
      '<div><span>{{ title }}</span><slot name="header" /><slot /><slot name="footer" /></div>',
  },
  'el-button': { template: '<button type="button"><slot /></button>' },
}

const VERSIONS = {
  document_id: 'doc-alpha',
  current_version: 3,
  items: [
    {
      version: 3,
      status: 'current',
      checksum: 'c'.repeat(64),
      char_count: 12,
      created_at: '2026-11-03T10:00:00',
      content: null,
    },
    {
      version: 2,
      status: 'archived',
      checksum: 'b'.repeat(64),
      char_count: 21,
      created_at: '2026-11-02T10:00:00',
      content: null,
    },
    {
      version: 1,
      status: 'archived',
      checksum: 'a'.repeat(64),
      char_count: 9,
      created_at: '2026-11-01T10:00:00',
      content: null,
    },
  ],
}

function mountDialog(props: { busy?: boolean } = {}) {
  return mount(DocumentVersionsDialog, {
    props: {
      modelValue: true,
      documentId: 'doc-alpha',
      documentTitle: '积分规则',
      ...props,
    },
    global: { stubs: STUBS },
  })
}

function rollbackButtons(wrapper: ReturnType<typeof mountDialog>) {
  return wrapper.findAll('button').filter((button) => button.text().includes('回滚到此稿'))
}

beforeEach(() => {
  mocks.fetchVersions.mockReset()
  mocks.rollback.mockReset()
  mocks.fetchVersions.mockResolvedValue(VERSIONS)
  // 回执按**被请求的那一稿**回答：写死一个稿号会让「参数带错」这类接线错误看起来正常。
  mocks.rollback.mockImplementation((documentId: string, version: number) =>
    Promise.resolve({ document_id: documentId, version, previous_version: 3, status: 3 }),
  )
})

describe('DocumentVersionsDialog：列表', () => {
  it('打开时读一次版本并显示稿号、字符数与时间', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    expect(mocks.fetchVersions).toHaveBeenCalledTimes(1)
    expect(mocks.fetchVersions).toHaveBeenCalledWith('doc-alpha')

    const rows = wrapper.findAll('tbody tr')
    expect(rows).toHaveLength(3)
    expect(rows[0].text()).toContain('v3')
    expect(rows[0].text()).toContain('当前')
    expect(rows[1].text()).toContain('21 字符')
    // 时间按本地时区显示到分钟；不显示原始 ISO 串（其中那一截 UTC 偏移会误导读时间的人）。
    expect(rows[1].text()).toContain('2026-11-02 10:00')
  })

  it('当前稿没有回滚按钮，其余两稿各有一个', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    expect(rollbackButtons(wrapper).map((button) => button.text())).toEqual([
      '回滚到此稿',
      '回滚到此稿',
    ])
  })

  it('文档正在索引时不给回滚按钮（后端会回 20025）', async () => {
    const wrapper = mountDialog({ busy: true })
    await flushPromises()

    expect(rollbackButtons(wrapper)).toHaveLength(0)
  })

  it('读取失败时把后端原话显示出来，而不是假装空列表', async () => {
    mocks.fetchVersions.mockRejectedValue(new Error('这条文档不存在'))
    const wrapper = mountDialog()
    await flushPromises()

    expect(wrapper.text()).toContain('这条文档不存在')
    expect(wrapper.findAll('tbody tr')).toHaveLength(0)
  })
})

describe('DocumentVersionsDialog：回滚确认', () => {
  it('点「回滚到此稿」先出确认框，且明说会影响所有调用方', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    await rollbackButtons(wrapper)[0].trigger('click')

    expect(wrapper.text()).toContain('回滚到第 2 稿')
    expect(wrapper.text()).toContain('会影响所有调用方')
    // 还没确认，就一次接口都不该发。
    expect(mocks.rollback).not.toHaveBeenCalled()
  })

  it('确认后按被点的那一稿回滚，并通知页面刷新', async () => {
    const wrapper = mountDialog()
    await flushPromises()

    await rollbackButtons(wrapper)[1].trigger('click')
    const confirm = wrapper
      .findAll('button')
      .find((button) => button.text().includes('确认回滚'))
    expect(confirm, '找不到确认回滚按钮').toBeDefined()
    await confirm!.trigger('click')
    await flushPromises()

    expect(mocks.rollback).toHaveBeenCalledTimes(1)
    expect(mocks.rollback).toHaveBeenCalledWith('doc-alpha', 1)
    expect(wrapper.emitted('rolledBack')).toEqual([[1]])
  })

  it('后端拒绝时把业务码给回来，并且不发第二次请求', async () => {
    mocks.rollback.mockRejectedValue(new Error('这一稿已经是当前稿'))
    const wrapper = mountDialog()
    await flushPromises()

    await rollbackButtons(wrapper)[0].trigger('click')
    await wrapper
      .findAll('button')
      .find((button) => button.text().includes('确认回滚'))!
      .trigger('click')
    await flushPromises()

    expect(mocks.rollback).toHaveBeenCalledTimes(1)
    expect(wrapper.emitted('rolledBack')).toBeUndefined()
    // 失败之后列表要重读一次：可能这一稿刚刚被别人删掉（20030 / 20032）。
    expect(mocks.fetchVersions).toHaveBeenCalledTimes(2)
  })
})
