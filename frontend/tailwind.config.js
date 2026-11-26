/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      /**
       * 设计令牌的唯一出处是 `src/styles/tokens.css`。
       *
       * 这里只做映射：**颜色一律走 `rgb(var(--c-x) / <alpha-value>)`**。
       * 这样 `bg-surface/85`、`border-line/70` 这些带透明度的写法照旧能用
       * （直接写 `var(--x)` 的话 Tailwind 的 `/xx` 会失效）。
       *
       * `slate` 也被整体重映射到同一套灰阶变量——页面里有几百处 `text-slate-500`，
       * 深色下靠变量反转，不用逐个改（语义「次要一点」保持不变）。
       */
      colors: {
        canvas: 'rgb(var(--c-canvas) / <alpha-value>)',
        surface: 'rgb(var(--c-surface) / <alpha-value>)',
        'surface-muted': 'rgb(var(--c-surface-muted) / <alpha-value>)',
        'surface-sunken': 'rgb(var(--c-surface-sunken) / <alpha-value>)',

        ink: 'rgb(var(--c-ink) / <alpha-value>)',
        muted: 'rgb(var(--c-muted) / <alpha-value>)',
        faint: 'rgb(var(--c-faint) / <alpha-value>)',
        disabled: 'rgb(var(--c-disabled) / <alpha-value>)',

        line: 'rgb(var(--c-line) / <alpha-value>)',
        'line-strong': 'rgb(var(--c-line-strong) / <alpha-value>)',

        accent: {
          DEFAULT: 'rgb(var(--c-accent) / <alpha-value>)',
          hover: 'rgb(var(--c-accent-hover) / <alpha-value>)',
          soft: 'rgb(var(--c-accent-soft) / <alpha-value>)',
        },
        brand: 'rgb(var(--c-brand) / <alpha-value>)',
        danger: 'rgb(var(--c-danger) / <alpha-value>)',
        star: 'rgb(var(--c-star) / <alpha-value>)',

        slate: {
          50: 'rgb(var(--c-slate-50) / <alpha-value>)',
          100: 'rgb(var(--c-slate-100) / <alpha-value>)',
          200: 'rgb(var(--c-slate-200) / <alpha-value>)',
          300: 'rgb(var(--c-slate-300) / <alpha-value>)',
          400: 'rgb(var(--c-slate-400) / <alpha-value>)',
          500: 'rgb(var(--c-slate-500) / <alpha-value>)',
          600: 'rgb(var(--c-slate-600) / <alpha-value>)',
          700: 'rgb(var(--c-slate-700) / <alpha-value>)',
          800: 'rgb(var(--c-slate-800) / <alpha-value>)',
          900: 'rgb(var(--c-slate-900) / <alpha-value>)',
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
        card: '24px', // 卡片：设计稿的 24px
        control: '14px', // 输入框、下拉、次级按钮
        small: '10px',
        pill: '980px', // 药丸：按钮、标签、导航
      },
      boxShadow: {
        // 玻璃的两层投影：贴地的一层硬影定形，大半径负传播的一层给浮起
        card: '0 1px 3px rgb(24 33 52 / 0.07), 0 18px 32px -22px rgb(24 33 52 / 0.3), 0 34px 64px -36px rgb(24 33 52 / 0.3)',
        lift: '0 2px 8px rgb(24 33 52 / 0.1), 0 26px 44px -26px rgb(24 33 52 / 0.36), 0 48px 88px -44px rgb(24 33 52 / 0.36)',
        nav: '0 1px 2px rgb(24 33 52 / 0.07), 0 8px 20px -14px rgb(24 33 52 / 0.3)',
        sheen: 'inset 0 1px 0 rgb(255 255 255 / 0.92)',
      },
      letterSpacing: {
        tightest: '-0.03em',
      },
      maxWidth: {
        page: '1120px', // 内容列宽度
      },
      transitionTimingFunction: {
        glass: 'cubic-bezier(0.2, 0.8, 0.2, 1)',
      },
      keyframes: {
        // 面进入：从模糊到清晰，比淡入更像「材料落定」
        'glass-in': {
          from: { opacity: '0', transform: 'scale(0.988) translateY(6px)', filter: 'blur(6px)' },
          to: { opacity: '1', transform: 'none', filter: 'blur(0)' },
        },
      },
      animation: {
        'glass-in': 'glass-in 220ms cubic-bezier(0.2, 0.8, 0.2, 1) both',
      },
    },
  },
  plugins: [],
}
