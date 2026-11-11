// @vitest-environment jsdom
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import UploadPickerDialog from '../UploadPickerDialog.vue'

/**
 * 「选择文件夹」按钮必须真的打开目录选择器。
 *
 * 真实故障（2026-11-12）：按钮写了 `@click="folderInput?.click()"`，而那个隐藏的目录
 * input 用的是**函数 ref**（`:ref="(el) => bindFolderInput(el)"`），只设置了
 * `webkitdirectory`，从没把元素写进 `folderInput`。于是 `folderInput` 恒为 `null`，
 * 可选链把这次点击吞掉——**点了就是没反应**，控制台连报错都没有。
 *
 * 为什么它长期没被发现：前端此前**没有任何组件测试**，这条接线只靠手点验证，而手点
 * 「选择文件」（那条 ref 写对了）一切正常，很容易以为整块都好着。
 *
 * 用 Element Plus 的桩而不是真组件：这里要验的是「模板里的 ref 接线」，不是弹窗长什么样；
 * 引整个 ElementPlus 进 jsdom 只会让这条断言更难读、更容易因样式问题挂掉。
 */

const STUBS = {
  'el-dialog': { template: '<div><slot /><slot name="footer" /></div>' },
  // 不 `$emit('click')`：Vue 会把未声明的监听器透传到根元素，再加上手动 emit 就会触发
  // 两次。这里只留原生按钮，让父组件的 `@click` 走正常的事件透传。
  'el-button': { template: '<button type="button"><slot /></button>' },
  'el-select': { template: '<div><slot /></div>' },
  'el-option': { template: '<div />' },
}

function mountDialog() {
  return mount(UploadPickerDialog, {
    props: { modelValue: true, kbId: 'kb-1', kbOptions: [] },
    global: { stubs: STUBS },
  })
}

/** 两个 file input 的顺序与模板一致：先「选择文件」，后「选择文件夹」。 */
function fileInputs(wrapper: ReturnType<typeof mountDialog>) {
  return wrapper.findAll('input[type="file"]')
}

describe('UploadPickerDialog：目录选择的接线', () => {
  it('「选择文件夹」按钮会去点那个目录 input', async () => {
    const wrapper = mountDialog()
    const folder = fileInputs(wrapper)[1]
    const clickSpy = vi.spyOn(folder.element as HTMLInputElement, 'click')

    const button = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('选择文件夹'))
    expect(button, '找不到「选择文件夹」按钮').toBeDefined()
    await button!.trigger('click')

    // 核心断言：这一次点击必须落到目录 input 上。
    expect(clickSpy).toHaveBeenCalledTimes(1)
  })

  it('目录 input 真的带上了 webkitdirectory 属性', () => {
    const wrapper = mountDialog()
    const folder = fileInputs(wrapper)[1]

    // 没有这个属性，选出来的是「一堆散文件」，`webkitRelativePath` 全是空串，
    // 子目录里的同名文件就分不出来了。
    expect(folder.attributes('webkitdirectory')).toBeDefined()
  })

  it('「选择文件」按钮仍然工作（别把对的那条改坏）', async () => {
    const wrapper = mountDialog()
    const plain = fileInputs(wrapper)[0]
    const clickSpy = vi.spyOn(plain.element as HTMLInputElement, 'click')

    const button = wrapper
      .findAll('button')
      .find((candidate) => candidate.text().includes('选择文件'))
    await button!.trigger('click')

    expect(clickSpy).toHaveBeenCalledTimes(1)
  })
})
