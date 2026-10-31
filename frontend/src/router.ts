import { createRouter, createWebHashHistory } from 'vue-router'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', component: () => import('@/views/Home.vue') },
    { path: '/quickchat', component: () => import('@/views/QuickChatView.vue') },
    { path: '/settings', component: () => import('@/views/Settings.vue') },
    { path: '/capabilities', component: () => import('@/views/CapabilityManagement.vue') },
    { path: '/extensions', component: () => import('@/views/ExtensionManagement.vue') },
    { path: '/plugins', redirect: '/capabilities' }  // 向后兼容旧链接
  ]
})
