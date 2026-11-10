/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  theme: {
    extend: {
      colors: {
        // 设计令牌的唯一出处（事实底座 §10）；页面里不再各写一遍十六进制。
        brand: '#045B89',
        canvas: '#F9FAFB',
        line: '#E5E7EB',
        danger: '#FA5252',
        star: '#FBC024',
      },
    },
  },
  plugins: [],
}
