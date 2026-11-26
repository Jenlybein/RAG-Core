/**
 * Vite 的 `?raw` 导入：拿到文件的原始文本。
 *
 * 声明在这里是因为 `vue-tsc --noEmit` 会检查**测试文件**，而容器里的依赖集与本地不同
 * （镜像不做全量 dev 安装）——用 `node:fs` 读源码在那里没有类型，用 `?raw` 则两边一致。
 */
declare module '*?raw' {
  const content: string
  export default content
}
