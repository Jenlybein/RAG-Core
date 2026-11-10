/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      /**
       * 设计令牌的唯一出处。
       *
       * 这套界面借的是「暖纸张 + 深墨」的编辑部气质（米白底、白卡、极淡的暖灰描边、
       * 近黑的实心按钮），再叠上苹果那套克制：大留白、几乎看不见的投影、字号层级拉开、
       * 半粗标题收紧字距。**颜色的职责不是装饰，是分层**：canvas 与 surface 只差一点点
       * 明度，页面因此像一张纸上的卡片，而不是一堆积木。
       *
       * `slate` 被整体重映射成暖灰。这一条改动量最大、也最划算：17 个文件里成百个
       * `text-slate-500` / `bg-slate-50` 不用逐个去改，语义（次要文字、浅底块）保持原样，
       * 色彩一起暖过来。若只在页面上贴一层米色背景而不动文字色，冷灰字压在暖底上会
       * 显脏——这是这类改版最容易翻车的地方。
       */
      colors: {
        // 底色与面
        canvas: '#F7F5F0', // 页面底：暖米白
        surface: '#FFFFFF', // 卡片面
        'surface-muted': '#FBFAF6', // 卡片内的次级块（表头、代码块）
        // 文字
        ink: '#1C1A15', // 主文字：不是纯黑，纯黑在暖底上过硬
        /**
         * 次要文字。取 #6B6558 而不是更浅的 #8A8275：后者在白卡上只有约 3.8:1，
         * 低于正文 4.5:1 的门槛（说明文字大量用在 12–14px，字号小就更吃对比度）。
         * 这个值 ≈5.5:1，仍是灰的，但不用眯眼。
         */
        muted: '#6B6558',
        // 描边（两级：一处可省略，一处必须有）
        line: '#E7E2D7',
        'line-strong': '#D9D2C3',
        // 实心按钮与强调
        accent: {
          DEFAULT: '#2C2921',
          hover: '#413C31',
          soft: '#F0ECE2', // 浅强调底（选中项、标签）
        },
        danger: '#B4392F', // 暖化的红：原 #FA5252 在米白上过于荧光
        star: '#E0A32E',
        // 重映射：整站冷灰 → 暖灰（同样按对比度调过：500 的 ~5.2:1 才够小字说明用）
        slate: {
          50: '#FBFAF6',
          100: '#F4F1E9',
          200: '#E7E2D7',
          300: '#D5CEC0',
          400: '#9A9285',
          500: '#736C5E',
          600: '#5A5348',
          700: '#453F35',
          800: '#2C2921',
          900: '#1C1A15',
        },
      },
      fontFamily: {
        // 苹果的字体栈 + 中文回落到苹方：不引外部字体（内部工具，不值得一次 500KB 的下载）
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"SF Pro Text"',
          '"Segoe UI"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei"',
          'sans-serif',
        ],
        mono: ['"SF Mono"', '"JetBrains Mono"', 'Menlo', 'Consolas', 'monospace'],
      },
      borderRadius: {
        card: '20px', // 卡片
        control: '12px', // 输入框、下拉、次级按钮
      },
      boxShadow: {
        // 两层：一层贴边定形，一层大范围散开——苹果那种「几乎看不见但物体确实浮着」的观感
        card: '0 1px 2px rgba(28, 26, 21, 0.04), 0 12px 32px -18px rgba(28, 26, 21, 0.18)',
        lift: '0 2px 6px rgba(28, 26, 21, 0.06), 0 18px 48px -24px rgba(28, 26, 21, 0.24)',
      },
      letterSpacing: {
        tightest: '-0.03em',
      },
      maxWidth: {
        page: '1180px', // 比 5xl 宽一点：表格类页面在 5xl 下太挤
      },
    },
  },
  plugins: [],
}
