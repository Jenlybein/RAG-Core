/**
 * 调试台改造的结构守卫：Key 从弹窗搬进主面板、不再拉 Key 列表、选库弹窗只剩库。
 *
 * ## 守的是什么
 *
 * 1. **Key 是主面板常驻输入框**：应用成功 / 失败都在这一屏自证（「已应用」或后端原话），
 *    不靠弹窗、也不靠列表。反例是回到「点按钮弹一个粘贴框」：粘完就得关窗，用户看不到
 *    「现在用的是哪把」。
 * 2. **调试台不调 `GET /console/keys`**：列表里没有原文，而「这把 Key 有没有效」只有
 *    拿它打一次数据面请求才作数。拉一份列表回来再本地比对，会把「服务端还没说话」误当
 *    成「Key 不对」。
 * 3. **Key 失败不偷偷降级**：库清单拉不回来就把「选择知识库」置灰，原始报错摊在输入框下面。
 * 4. **选库弹窗只剩库**：底部是「取消 / 确定」，勾选改草稿、确定才落 store。
 *
 * 这些判据挂载整页要拖四套替身，而每条只有一两句话，扫源码更快；接口与存储行为在
 * `lib/__tests__/apiKeySession.spec.ts`。
 */

import { describe, expect, it } from 'vitest'

import PAGE from '../RetrievePage.vue?raw'
import SESSION from '../../lib/apiKeySession.ts?raw'

function count(source: string, needle: string): number {
  return source.split(needle).length - 1
}

describe('Key 改成主面板常驻输入框', () => {
  it('有输入框、「应用」按钮，回车也触发应用', () => {
    expect(PAGE).toContain('placeholder="粘贴 API Key（rc_live_…）"')
    expect(PAGE).toContain('type="password"')
    expect(PAGE).toContain('show-password')
    expect(PAGE).toContain('@click="applyKey()"')
    expect(PAGE).toContain('@keyup.enter="applyKey()"')
    expect(PAGE).toContain('>应用</el-button>')
  })

  it('应用成功给「已应用」；失败摊后端原话，并把选库按钮置灰', () => {
    expect(PAGE).toContain('已应用')
    expect(PAGE).toContain('keyVerified')
    // 失败文案取后端原文（数据面 20010 有四种成因，通用文案会盖掉唯一有用的那句）。
    expect(PAGE).toContain('backendErrorText')
    expect(PAGE).toContain('keyApplyError')
    expect(PAGE).toContain('text-red-700')
    // 不允许选库：置灰 + openKbDialog 自己再挡一道。
    expect(PAGE).toContain(':disabled="pickerDisabled"')
    expect(PAGE).toContain('if (pickerDisabled.value) return')
  })

  it('应用时先清空已选库并提示「Key 已更换，请重新选库」', () => {
    const apply = PAGE.slice(PAGE.indexOf('function applyKey'), PAGE.indexOf('function applyKey') + 1600)
    expect(apply).toContain('store.selectedCount > 0')
    expect(apply).toContain('allowedKbIds: []')
    expect(apply).toContain("ElMessage.info('Key 已更换，请重新选库')")
    // 顺序：先写请求层与清空选择，再让查询键 +1 去拉库清单。
    expect(apply.indexOf('store.selectKey')).toBeLessThan(apply.indexOf('applySerial.value += 1'))
  })

  it('旁边有「去 API Key 页获取」直达 /keys', () => {
    expect(PAGE).toContain('去 API Key 页获取')
    expect(PAGE).toContain("router.push('/keys')")
  })

  it('sessionStorage 键存在，且挂载时预填 + 自动应用一次', () => {
    expect(PAGE).toContain('readRetrieveApiKey()')
    expect(PAGE).toContain('writeRetrieveApiKey(raw)')
    expect(PAGE).toContain('if (initialStoredKey !== null) applyKey({ silent: true })')
    expect(SESSION).toContain("RETRIEVE_API_KEY_STORAGE_KEY = 'rag-console:api-key'")
    expect(SESSION).toContain('sessionStorage')
  })
})

describe('不再拉 Key 列表', () => {
  it('调试台不 import keyService / 不用 KEYS_QUERY_KEY / 不调 fetchKeys', () => {
    expect(PAGE).not.toContain('fetchKeys')
    expect(PAGE).not.toContain('KEYS_QUERY_KEY')
    expect(PAGE).not.toContain("from '../services/keyService'")
    // `/keys` 页自己仍然用这个键——那条守卫在 `KeysPage.guards.spec.ts` 里。
  })

  it('旧的「粘贴 API Key」弹窗与「换一把 Key」按钮已删除', () => {
    expect(PAGE).not.toContain('title="粘贴 API Key"')
    expect(PAGE).not.toContain('换一把 Key')
    expect(PAGE).not.toContain('openPasteDialog')
    expect(PAGE).not.toContain('submitPastedKey')
    expect(PAGE).not.toContain('用它发起检索')
  })
})

describe('「选 Key 与库」改成「选择知识库」', () => {
  it('主面板有「选择知识库」按钮 + 已选摘要', () => {
    const entry = PAGE.indexOf('@click="openKbDialog"')
    expect(entry).toBeGreaterThan(0)
    expect(PAGE.slice(entry - 400, entry + 400)).toContain('选择知识库')
    expect(PAGE).toContain('selectedKbSummary(store.selectedCount)')
    // 摘要文案来自纯函数（`utils/retrieveSelection.ts`），模板不再自己拼。
    expect(PAGE).not.toContain('已选 {{ store.selectedCount }} 个库')
  })

  it('弹窗标题与内容只剩知识库；底部是取消 / 确定', () => {
    expect(PAGE).toContain('title="选择知识库"')
    expect(PAGE).toContain(':model-value="dialogSelection"')
    expect(PAGE).toContain('@update:model-value="onSelectionChange"')
    expect(PAGE).toContain('>取消</el-button>')
    expect(PAGE).toContain('>确定</el-button>')
    // 确定才落到 store；取消什么也不改（勾选只动草稿）。
    expect(PAGE).toContain('store.setSelection(dialogSelection.value)')
    // 弹窗里不再有 Key 那几块。
    const dialog = PAGE.slice(PAGE.indexOf('title="选择知识库"'), PAGE.indexOf('</el-dialog>'))
    expect(dialog).not.toContain('当前 Key')
    expect(dialog).not.toContain('粘贴 API Key')
    expect(dialog).not.toContain('换一把 Key')
    expect(dialog).not.toContain('密钥配置（只读）')
  })

  it('旧入口「选 Key 与库」「修改配置」不再出现', () => {
    expect(PAGE).not.toContain('选 Key 与库')
    expect(PAGE).not.toContain('修改配置')
    expect(PAGE).not.toContain('title="修改检索配置"')
  })
})

describe('开发身份模式提示条', () => {
  it('按新的说法解释：这里填任何值都能看到全部知识库，检索不按白名单/额度过滤', () => {
    expect(PAGE).toContain(
      '当前是开发身份模式：这里填任何值都能看到全部知识库，检索不会按它的白名单或额度过滤。',
    )
    // 反例：旧文案「所选 Key 不参与鉴权」——它说不清「那我该填什么」。
    expect(PAGE).not.toContain('所选 Key 不参与鉴权')
  })
})

describe('能力选择不再按授权位置灰', () => {
  it('能力按钮始终可选（没有 Key 列表就没有授权位，不能把「不知道」显示成「不行」）', () => {
    expect(PAGE).not.toContain('capabilityAllowed')
    // 反例：能力单选按 policy 的授权位置灰——本页已经拿不到 policy。
    expect(PAGE).not.toContain(':disabled="policy !== null')
    expect(count(PAGE, 'option.hint')).toBeGreaterThan(0)
  })
})
