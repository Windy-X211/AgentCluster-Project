import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import router from './router'
import { isDesktop } from './utils/env'
import './styles/liquid.scss'

// 桌面无边框窗口标记：在应用入口添加，保证任意路由（含刷新直接进入设置/能力管理页）都生效
if (isDesktop) document.documentElement.classList.add('desktop-frameless')

createApp(App).use(createPinia()).use(router).mount('#app')
