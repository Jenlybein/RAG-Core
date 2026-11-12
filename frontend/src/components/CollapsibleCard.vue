<script setup lang="ts">
/**
 * 可折叠卡片：整站「一块内容」的标准外壳。
 *
 * 为什么不用原生 `<details>` / `el-collapse`：
 *
 * - `<details>` 的 `<summary>` 里放按钮是**交互嵌套**——点「刷新」会顺带把面板收起，
 *   而 `@click.stop` 只是把问题藏起来（键盘 Tab 到按钮再回车仍然会触发 summary）。
 * - `el-collapse` 的标题区同理，且它自带一层内边距与边框，与 `.card` 撞样式。
 *
 * 所以这里把「折叠开关」和「操作区」拆成两个并列元素：左边按钮负责开合，右边插槽
 * 里的按钮只做自己的事。两件事互不干扰，键盘也各自可达。
 *
 * 折叠状态是组件内部的 `ref`，不落 URL / storage：刷新回到默认态。这些面板是调试用的
 * 辅助区，不是用户要记住的偏好。
 */
import { ref } from 'vue'

const props = withDefaults(
  defineProps<{
    title: string
    /** 标题右下方的一行说明；不传就不留空行。 */
    subtitle?: string
    /** 初始是否展开。诊断类面板传 `false`，主路径上的内容保持展开。 */
    defaultOpen?: boolean
    /** 收起时右上角仍然显示的小结（例如「3 条」），让人不必展开也知道里面是什么。 */
    summary?: string
  }>(),
  { subtitle: '', defaultOpen: true, summary: '' },
)

const open = ref(props.defaultOpen)
</script>

<template>
  <section class="card overflow-hidden">
    <header class="flex items-start justify-between gap-3 px-5 py-3.5">
      <button
        type="button"
        class="flex min-w-0 items-start gap-2.5 text-left"
        :aria-expanded="open"
        @click="open = !open"
      >
        <span
          class="mt-0.5 shrink-0 text-[11px] leading-5 text-muted transition-transform duration-150"
          :class="open ? 'rotate-90' : ''"
          aria-hidden="true"
          >▶</span
        >
        <span class="min-w-0">
          <span class="section-title block truncate">{{ title }}</span>
          <span v-if="subtitle" class="mt-0.5 block text-xs text-muted">{{ subtitle }}</span>
        </span>
      </button>

      <div class="flex shrink-0 flex-wrap items-center justify-end gap-2">
        <span v-if="!open && summary !== ''" class="chip">{{ summary }}</span>
        <slot name="actions" />
      </div>
    </header>

    <div v-show="open" class="border-t border-line px-5 py-4">
      <slot />
    </div>
  </section>
</template>
