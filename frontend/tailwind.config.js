/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      /**
       * 设计令牌的唯一出处。
       *
       * 这一版按 **apple.com 的口径**重设：浅灰底（#f5f5f7）+ 纯白卡 + 近黑墨（#1d1d1f）
       * + 苹果蓝（#0071e3）做唯一的强调色。三条与上一版（暖纸 + 深墨）的关键差别：
       *
       * 1. **去掉暖色**：整站从「暖米白」切到「冷浅灰」。苹果的观感靠的是灰阶干净，
       *    任何一点黄调都会让白色卡片显得发旧。
       * 2. **强调色从「近黑」换成蓝**：苹果的操作是蓝的（`#0071e3`），黑色只留给标题与
       *    正文。上一版把按钮做成近黑实底，更像「纸张 + 印刷」；苹果更像「屏幕 + 控件」。
       * 3. **边框几乎退场**：卡片的形来自**底色差 + 极淡投影**，不是 1px 描边。`line`
       *    这一档只留给表格分隔与输入框。
       *
       * `slate` 继续整体重映射（这次映射到苹果的中性灰阶）：17 个文件里成百个
       * `text-slate-500` / `bg-slate-50` 不用逐个改，语义（次要文字、浅底块）保持原样，
       * 色彩一起中性化。若不映射，冷灰与暖灰会混在一屏里显脏。
       */
      colors: {
        // 底色与面
        canvas: '#f5f5f7', // 页面底：苹果的浅灰
        surface: '#ffffff', // 卡片面：纯白
        'surface-muted': '#fafafc', // 卡片内次级块（表头、代码块）
        // 文字
        ink: '#1d1d1f', // 主文字：苹果的「近黑」
        /** 次要文字。#6e6e73 是苹果正文灰，对比度 ≈5.2:1，小字也够。 */
        muted: '#6e6e73',
        // 描边（两级：一处可省略，一处必须有）
        line: '#d2d2d7', // 苹果的分隔线灰
        'line-strong': '#b8b8bf',
        // 实心按钮与强调
        accent: {
          DEFAULT: '#0071e3', // 苹果蓝
          hover: '#0077ed',
          soft: '#e8f2ff', // 浅蓝底（选中项、标签）
        },
        /** `brand` 是「链接 / 强调」的语义别名，与 accent 相同。 */
        brand: '#0071e3',
        danger: '#d70015', // 苹果的按钮红（比 #ff3b30 更适合小字）
        star: '#ff9f0a',
        // 重映射：整站冷/暖灰 → 苹果中性灰阶
        slate: {
          50: '#f5f5f7',
          100: '#e8e8ed',
          200: '#d2d2d7',
          300: '#b8b8bf',
          400: '#86868b',
          500: '#6e6e73',
          600: '#515154',
          700: '#3a3a3c',
          800: '#1d1d1f',
          900: '#000000',
        },
      },
      fontFamily: {
        // 苹果的字体栈 + 中文回落到苹方：不引外部字体（内部工具，不值得一次 500KB 的下载）
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"SF Pro Text"',
          '"Helvetica Neue"',
          '"Segoe UI"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei"',
          'sans-serif',
        ],
        mono: ['"SF Mono"', '"JetBrains Mono"', 'Menlo', 'Consolas', 'monospace'],
      },
      borderRadius: {
        card: '18px', // 卡片：苹果常用 18~20px
        control: '12px', // 输入框、下拉、次级按钮
        pill: '980px', // 苹果的按钮形状
      },
      boxShadow: {
        // 苹果的卡片几乎看不见投影：靠白色与浅灰底的反差定形，投影只负责一点点浮起
        card: '0 2px 8px rgba(0, 0, 0, 0.04), 0 12px 32px -20px rgba(0, 0, 0, 0.12)',
        lift: '0 4px 16px rgba(0, 0, 0, 0.08), 0 24px 56px -24px rgba(0, 0, 0, 0.28)',
      },
      letterSpacing: {
        tightest: '-0.03em',
      },
      maxWidth: {
        page: '1120px', // 苹果的内容列宽度
      },
    },
  },
  plugins: [],
}
