import { VueQueryPlugin } from '@tanstack/vue-query'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import './styles/main.css'

const app = createApp(App)

// Pinia 现在只放骨架；T10 的「选中 Key 只进内存」会依赖它，所以底座先立起来。
app.use(createPinia())
app.use(router)
// 数据请求都走两个手写客户端；query 留给后续页面的缓存与轮询（T3/T4）。
app.use(VueQueryPlugin)
app.use(ElementPlus)

app.mount('#app')
